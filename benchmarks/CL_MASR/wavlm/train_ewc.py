#!/usr/bin/env python3

"""Recipe for fine-tuning a WavLM-based ASR system on Common Voice in a continual
learning fashion via Elastic Weight Consolidation (https://arxiv.org/abs/1612.00796).

To run this recipe, do the following:
> python train_ewc.py hparams/train_ewc.yaml

Authors
 * Luca Della Libera 2023
"""

import logging
import os
import pathlib
import sys
import time

import torch
import torchaudio
from hyperpyyaml import load_hyperpyyaml

import speechbrain as sb
from speechbrain.utils.distributed import run_on_main

from common_voice_prepare import prepare_common_voice


class ASR(sb.Brain):
    def compute_forward(self, batch, stage):
        """Forward computations from the waveform batches to the output probabilities."""
        batch = batch.to(self.device)
        wavs, wav_lens = batch.sig
        tokens, _ = batch.tokens

        # Forward encoder + projection
        if self.hparams.gradient_checkpointing:
            wavs.requires_grad_()
            logits = torch.utils.checkpoint.checkpoint(
                self.modules.wavlm, wavs, wav_lens,
            )
        else:
            logits = self.modules.wavlm(wavs, wav_lens)

        hyps = None
        if stage != sb.Stage.TRAIN:
            hyps = sb.decoders.ctc_greedy_decode(
                logits, wav_lens, blank_id=self.hparams.blank_index
            )

        return logits, hyps

    def compute_objectives(self, predictions, batch, stage):
        """Computes the loss given predictions and targets."""
        _, wav_lens = batch.sig
        logits, hyps = predictions
        ids = batch.id
        tokens, tokens_lens = batch.tokens

        logits = logits.float()  # Force float32 when using mixed precision
        log_probs = logits.log_softmax(dim=-1)
        loss = self.hparams.ctc_loss(log_probs, tokens, wav_lens, tokens_lens)

        if stage == sb.Stage.TRAIN and hasattr(self.hparams, "all_ewc_params"):
            for name, param in self.modules.wavlm.named_parameters():
                if not param.requires_grad or param.grad is None:
                    continue
                for (old_param, fisher) in self.hparams.all_ewc_params:
                    loss += (
                        (
                            fisher[name]
                            * (old_param[name] - param.to(fisher[name].device))
                            ** 2
                        )
                        .sum()
                        .to(self.device)
                        * 0.5
                        * self.hparams.ewc_lambda
                    )

        if stage != sb.Stage.TRAIN:
            target_words = batch.target_wrd

            # Decode predicted tokens to words
            predicted_words = self.tokenizer.decode(hyps)
            predicted_words = [text.split(" ") for text in predicted_words]

            self.wer_metric.append(ids, predicted_words, target_words)
            self.cer_metric.append(ids, predicted_words, target_words)

        return loss

    def on_stage_start(self, stage, epoch=None):
        """Gets called at the beginning of each epoch."""
        if stage != sb.Stage.TRAIN:
            self.cer_metric = self.hparams.cer_computer()
            self.wer_metric = self.hparams.wer_computer()

    def on_stage_end(self, stage, stage_loss, epoch=None):
        """Gets called at the end of an epoch."""
        # Compute/store important stats
        stage_stats = {"loss": stage_loss}
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats
        else:
            stage_stats["CER"] = self.cer_metric.summarize("error_rate")
            stage_stats["WER"] = self.wer_metric.summarize("error_rate")

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:
            old_lr, new_lr = self.hparams.lr_annealing(stage_stats["loss"])
            sb.nnet.schedulers.update_learning_rate(self.optimizer, new_lr)
            stats_meta_data = {
                "epoch": epoch,
                "lr": old_lr,
            }
            self.hparams.train_logger.log_stats(
                stats_meta=stats_meta_data,
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"WER": stage_stats["WER"]}, min_keys=["WER"],
            )
        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.hparams.epoch_counter.current},
                test_stats=stage_stats,
            )
            with open(self.hparams.wer_file, "w", encoding="utf-8") as w:
                self.wer_metric.write_stats(w)


class EWCParamsComputer(ASR):
    def zero_grad(self, set_to_none=False):
        """Do not reset gradients (required for EWC parameters computation)."""
        pass

    def fit_batch(self, batch):
        """Fit one batch."""
        outputs = self.compute_forward(batch, sb.Stage.TRAIN)
        loss = self.compute_objectives(outputs, batch, sb.Stage.TRAIN)
        with self.no_sync(False):
            loss.backward()
        self.on_fit_batch_end(batch, outputs, loss, True)
        return loss.detach().cpu()


def compute_ewc_params(hparams, run_opts, locales):
    """Compute EWC parameters (current parameter values + Fisher information matrix)
    for the given locales.

    Arguments
    ---------
    hparams : dict
        The hyperparameters.
    run_opts : dict
        The runtime options.
    locales : list[str]
        The locales to consider.

    """
    tokenizer = hparams["wavlm"].tokenizer
    batch_size = hparams["train_dataloader_kwargs"].get("batch_size", 1)
    max_grad_norm = hparams.get("max_grad_norm", 5.0)
    grad_accumulation_factor = hparams.get("grad_accumulation_factor", 1)
    auto_mix_prec = hparams.get("auto_mix_prec", False)
    hparams["train_dataloader_kwargs"]["batch_size"] = 1
    hparams["max_grad_norm"] = float("inf")
    hparams["auto_mix_prec"] = False
    hparams["grad_accumulation_factor"] = 1

    # Multi-gpu (ddp) save data preparation
    run_on_main(
        prepare_common_voice,
        kwargs={
            "locales": locales,
            "data_folder": hparams["data_folder"],
            "max_durations": hparams["max_durations"],
        },
    )

    # Create datasets, tokenization and encoding
    train_data, _, _ = dataio_prepare(hparams, tokenizer)

    # Trainer initialization
    asr_brain = EWCParamsComputer(
        modules=hparams["modules"], hparams=hparams, run_opts=run_opts,
    )

    # We dynamically add the tokenizer to our brain class
    asr_brain.tokenizer = tokenizer

    # Training (no parameter update)
    asr_brain.fit(
        range(1),
        train_data,
        train_loader_kwargs=hparams["train_dataloader_kwargs"],
    )

    params, fisher = {}, {}
    with torch.no_grad():
        for name, param in hparams["wavlm"].named_parameters():
            if not param.requires_grad or param.grad is None:
                continue
            params[name] = param.clone().cpu()
            fisher[name] = (param.grad.clone() ** 2).cpu()
    hparams["wavlm"].zero_grad(set_to_none=True)

    hparams["train_dataloader_kwargs"]["batch_size"] = batch_size
    hparams["max_grad_norm"] = max_grad_norm
    hparams["auto_mix_prec"] = auto_mix_prec
    hparams["grad_accumulation_factor"] = grad_accumulation_factor

    return params, fisher


def dataio_prepare(hparams, tokenizer):
    """This function prepares the datasets to be used in the brain class.
    It also defines the data processing pipeline through user-defined functions."""
    train_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=os.path.join(hparams["data_folder"], "train.csv"),
        replacements={"data_root": hparams["data_folder"]},
    )

    if hparams["sorting"] in ["descending", "ascending"]:
        # We sort training data to speed up training and get better results
        train_data = train_data.filtered_sorted(
            sort_key="duration",
            reverse=hparams["sorting"] == "descending",
            key_max_value={"duration": hparams["avoid_if_longer_than"]},
        )
        # When sorting do not shuffle in dataloader otherwise it is pointless
        hparams["train_dataloader_kwargs"]["shuffle"] = False

    elif hparams["sorting"] != "random":
        raise ValueError(
            f"`sorting` ({hparams['sorting']}) must be random, ascending or descending"
        )

    # reverse=True to fail fast in case of out-of-memory error
    valid_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=os.path.join(hparams["data_folder"], "dev.csv"),
        replacements={"data_root": hparams["data_folder"]},
    ).filtered_sorted(
        sort_key="duration",
        reverse=True,
        key_max_value={"duration": hparams["avoid_if_longer_than"]},
    )

    test_data = sb.dataio.dataset.DynamicItemDataset.from_csv(
        csv_path=os.path.join(hparams["data_folder"], "test.csv"),
        replacements={"data_root": hparams["data_folder"]},
    ).filtered_sorted(
        sort_key="duration",
        reverse=True,
        key_max_value={"duration": hparams["avoid_if_longer_than"]},
    )

    datasets = [train_data, valid_data, test_data]

    # 2. Define audio pipeline:
    @sb.utils.data_pipeline.takes("mp3")
    @sb.utils.data_pipeline.provides("sig")
    def audio_pipeline(mp3):
        info = torchaudio.info(mp3)
        sig = sb.dataio.dataio.read_audio(mp3)
        resampled = torchaudio.transforms.Resample(
            info.sample_rate, hparams["sample_rate"],
        )(sig)
        return resampled

    sb.dataio.dataset.add_dynamic_item(datasets, audio_pipeline)

    # 3. Define text pipeline:
    @sb.utils.data_pipeline.takes("wrd")
    @sb.utils.data_pipeline.provides("tokens", "target_wrd")
    def text_pipeline(wrd):
        tokens_list = tokenizer.encode(wrd)
        assert sum(i == hparams["blank_index"] for i in tokens_list) == 0
        tokens_list = tokens_list[: hparams["max_target_length"] - 1]
        tokens = torch.LongTensor(tokens_list)
        yield tokens
        wrd = wrd.split(" ")
        # When `ref_tokens` is an empty string add dummy space
        # to avoid division by 0 when computing WER/CER
        for i, char in enumerate(wrd):
            if len(char) == 0:
                wrd[i] = " "
        yield wrd

    sb.dataio.dataset.add_dynamic_item(datasets, text_pipeline)

    # 4. Set output:
    sb.dataio.dataset.set_output_keys(
        datasets, ["id", "sig", "tokens", "target_wrd"],
    )

    return train_data, valid_data, test_data


def test(hparams, run_opts, locales, wer_file="wer_test.txt"):
    """Test incrementally on the given locales.

    Arguments
    ---------
    hparams : dict
        The hyperparameters.
    run_opts : dict
        The runtime options.
    locales : list[str]
        The locales to test.
    wer_file : str
        The name of the file where WER results are saved.

    """
    # Test on base + new locales
    for locale in locales:
        # Multi-gpu (ddp) save data preparation
        run_on_main(
            prepare_common_voice,
            kwargs={
                "locales": [locale],
                "data_folder": hparams["data_folder"],
                "max_durations": hparams["max_durations"],
            },
        )

        if locale in ["zh-CN", "ja"]:
            # Use CER instead of WER (spaces are not used)
            hparams[
                "wer_computer"
            ] = lambda *args, **kwargs: sb.utils.metric_stats.ErrorRateStats(
                split_tokens=True
            )
        else:
            hparams["wer_computer"] = sb.utils.metric_stats.ErrorRateStats

        # Define tokenizer
        tokenizer = hparams["wavlm"].tokenizer

        # Create datasets, tokenization and encoding
        _, _, test_data = dataio_prepare(hparams, tokenizer)

        # Trainer initialization
        asr_brain = ASR(
            modules=hparams["modules"], hparams=hparams, run_opts=run_opts,
        )

        # We dynamically add the tokenizer to our brain class
        asr_brain.tokenizer = tokenizer

        # Testing
        locale_folder = os.path.join(hparams["output_folder"], locale)
        os.makedirs(locale_folder, exist_ok=True)
        asr_brain.hparams.wer_file = os.path.join(locale_folder, wer_file)
        if hparams["skip_test"]:
            # Dummy test
            train_log_backup = asr_brain.hparams.train_logger.save_file
            asr_brain.hparams.train_logger.save_file = (
                asr_brain.hparams.wer_file
            ) = os.path.join(locale_folder, "tmp.txt")
            test_data.data_ids = list(test_data.data.keys())[:1]
            test_data.data = {k: test_data.data[k] for k in test_data.data_ids}
            asr_brain.evaluate(
                test_data,
                min_key="WER",
                test_loader_kwargs=hparams["valid_dataloader_kwargs"],
            )
            os.remove(asr_brain.hparams.wer_file)
            asr_brain.hparams.train_logger.save_file = train_log_backup
            asr_brain.hparams.wer_file = os.path.join(locale_folder, wer_file)
        else:
            asr_brain.evaluate(
                test_data,
                min_key="WER",
                test_loader_kwargs=hparams["valid_dataloader_kwargs"],
            )

    # MACs not 100% accurate but still useful for comparisons
    if not hparams["skip_test"]:
        try:
            profile(hparams, run_opts)
        except Exception:
            logging.warning(
                "Install ptflops and torchinfo to profile the model (e.g. `pip install ptflops torchinfo`)"
            )


def train(hparams, run_opts):
    """Train incrementally on the new locales.

    Arguments
    ---------
    hparams : dict
        The hyperparameters.
    run_opts : dict
        The runtime options.

    """
    # Load checkpoint
    if hparams["pretrained_wavlm_path"] is not None:
        hparams["wavlm"].load_state_dict(
            torch.load(hparams["pretrained_wavlm_path"])
        )

    # Testing
    test(
        hparams, run_opts, hparams["base_locales"], f"wer_test_before.txt",
    )

    # Train on new locales
    # all_ewc_params = []
    for i, locale in enumerate(hparams["new_locales"]):
        # Remove old EWC parameters
        hparams.pop("all_ewc_params", None)

        # Compute new EWC parameters
        if not hparams["skip_ewc"]:
            if i == 0:
                ewc_params = compute_ewc_params(
                    hparams, run_opts, hparams["base_locales"]
                )
            else:
                ewc_params = compute_ewc_params(
                    hparams, run_opts, [hparams["new_locales"][i - 1]]
                )
            # all_ewc_params.append(ewc_params)
            all_ewc_params = [ewc_params]
            hparams["all_ewc_params"] = all_ewc_params

        # Multi-gpu (ddp) save data preparation
        run_on_main(
            prepare_common_voice,
            kwargs={
                "locales": [locale],
                "data_folder": hparams["data_folder"],
                "max_durations": hparams["max_durations"],
            },
        )

        # Define tokenizer
        tokenizer = hparams["wavlm"].tokenizer

        # Log total number of tokens
        logging.info(
            f"Total number of tokens: {hparams['wavlm'].model.decoder.out_proj.out_features}"
        )

        # Create datasets, tokenization and encoding
        train_data, valid_data, _ = dataio_prepare(hparams, tokenizer)

        # Trainer initialization
        checkpoint_folder = os.path.join(hparams["save_folder"], locale)
        os.makedirs(checkpoint_folder, exist_ok=True)
        hparams["checkpointer"].checkpoints_dir = pathlib.Path(
            checkpoint_folder
        )
        hparams["lr_annealing"].hyperparam_value = hparams["lr"]
        hparams["lr_annealing"].metric_values.clear()
        hparams["lr_annealing"].current_patient = 0
        asr_brain = ASR(
            modules=hparams["modules"],
            hparams=hparams,
            run_opts=run_opts,
            opt_class=hparams["opt_class"],
            checkpointer=hparams["checkpointer"],
        )

        # We dynamically add the tokenizer to our brain class
        asr_brain.tokenizer = tokenizer

        # Training
        hparams["valid_dataloader_kwargs"].pop("ckpt_prefix", None)
        hparams["epoch_counter"].current = 0
        asr_brain.fit(
            hparams["epoch_counter"],
            train_data,
            valid_data,
            train_loader_kwargs=hparams["train_dataloader_kwargs"],
            valid_loader_kwargs=hparams["valid_dataloader_kwargs"],
        )

        # Testing
        test(
            hparams,
            run_opts,
            hparams["base_locales"] + hparams["new_locales"][: i + 1],
            f"wer_test_after_{locale}.txt",
        )


def profile(hparams, run_opts):
    """Measure MACs, memory and inference time.

    Arguments
    ---------
    hparams : dict
        The hyperparameters.
    run_opts : dict
        The runtime options.

    """
    import ptflops
    import torchinfo

    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.wavlm = hparams["wavlm"]
            self.wavs = torch.randn(
                1, hparams["sample_rate"], device=run_opts["device"],
            )

        @torch.no_grad()
        def forward(self, _=None):
            logits = self.wavlm(self.wavs)
            return logits

    model = Model().eval().to(run_opts["device"])
    macs, params = ptflops.get_model_complexity_info(
        model, (1,), as_strings=True, print_per_layer_stat=False,
    )
    time_start = time.time()
    model()
    torch.cuda.synchronize()
    time_stop = time.time() - time_start
    max_mem = torch.cuda.max_memory_allocated("cuda") / 10 ** 9
    result = {
        "MACs": macs,
        "memory": max_mem,
        "time": time_stop,
    }
    logging.info(torchinfo.summary(model, verbose=0))
    logging.info(result)


if __name__ == "__main__":
    # Command-line interface
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])

    # If distributed_launch=True then
    # create ddp_group with the right communication protocol
    sb.utils.distributed.ddp_init_group(run_opts)

    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)
    hparams["train_logger"].save_file = hparams[
        "train_logger"
    ].save_file.replace(
        ".txt",
        f"_base={','.join(hparams['base_locales'])}_new={','.join(hparams['new_locales'])}.txt",
    )

    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_folder"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )

    # Train
    start_time = time.time()
    train(hparams, run_opts)
    duration = time.time() - start_time
    logging.info(f"Time elapsed: {duration} seconds")
