import os
import numpy as np
import sys
import torch
import logging
import speechbrain as sb
from speechbrain.utils.distributed import run_on_main
from hyperpyyaml import load_hyperpyyaml
from pathlib import Path
import torchaudio
from scipy.io import loadmat
from speechbrain.utils.parameter_transfer import Pretrainer
logger = logging.getLogger(__name__)


class Ultra_Brain(sb.Brain):
    def compute_forward(self, batch):
        #print('START')
        batch = batch.to(self.device)
        rf = batch.sig.data # removing the the length flag of the PaddedData type
        rf = rf.type(torch.cuda.FloatTensor)
        
        
        ### Normalization of input
        batch_size, height = rf.shape
        rf = rf.view(rf.size(0), -1)
        rf -= rf.min(1, keepdim=True)[0]
        rf /= rf.max(1, keepdim=True)[0]
        rf = rf.view(batch_size, height)
        
        
        #print('RF SIGNASL BEFOR',rf.shape)
        rf = rf.unsqueeze(dim=1)
        #print('RF SIGNASL',rf.shape)
        a = self.modules.CnnBlock(rf)
        logits = self.modules.MLPBlock(a)
        
        #print('OUT',logits)

        return logits 

    def compute_objectives(self, predictions, batch):
        #print('PREDICTION', predictions.shape, batch.att.shape )
        attenuation = batch.att
        attenuation = attenuation.type(torch.cuda.FloatTensor)
        return sb.nnet.losses.mse_loss(predictions, attenuation.unsqueeze(1))
    
    def fit_batch(self, batch):
        predictions = self.compute_forward(batch)
        #predictions = predictions.squeeze()
        #print('PREDICTION', predictions.shape, batch.att.shape )
        loss = self.compute_objectives(predictions, batch)
        loss.backward()
        if self.check_gradients(loss):
            self.optimizer.step()
        self.optimizer.zero_grad()
        return loss.detach()

    def evaluate_batch(self, batch,stage):
        if stage == sb.Stage.VALID or stage == sb.Stage.TEST:
            predictions = self.compute_forward(batch)
            with torch.no_grad():
                loss = self.compute_objectives(predictions, batch)
                #print("EVALUATE BATCH loss", loss)
            return loss.detach()

def dataio_prepare(hparams):
    """This function prepares the datasets to be used in the brain class.
    It also defines the data processing pipeline through user-defined functions."""
    # 1. Declarations:
    train_data = sb.dataio.dataset.DynamicItemDataset.from_json(
        json_path=hparams["train_json"],
    )
    if hparams["sorting"] == "ascending":
        # sorting data based on Attenuation!
        train_data = train_data.filtered_sorted(sort_key="attenuation")
        hparams["train_dataloader_opts"]["shuffle"] = False

    elif hparams["sorting"] == "descending":
        train_data = train_data.filtered_sorted(
            sort_key="attenuation", reverse=True
        )
        # when sorting do not shuffle in dataloader ! otherwise is pointless
        hparams["train_dataloader_opts"]["shuffle"] = False

    elif hparams["sorting"] == "random":
        pass

    else:
        raise NotImplementedError(
            "bebe! sorting must be random, ascending or descending"
        )

    valid_data = sb.dataio.dataset.DynamicItemDataset.from_json(
        json_path=hparams["valid_json"],
    )
    valid_data = valid_data.filtered_sorted(sort_key="attenuation")

    test_data = sb.dataio.dataset.DynamicItemDataset.from_json(
        json_path=hparams["test_json"],
    )
    test_data = test_data.filtered_sorted(sort_key="attenuation")


    datasets = [train_data, valid_data, test_data]
    #print('Train Data', train_data)

    def load_ultrasound(ULTRA_PATH):
        dic = {}
        data_dic = loadmat(ULTRA_PATH)
        try:
            dic['rf_data'] = data_dic['rf_data'].reshape((-1,))
            dic['rf_env'] = data_dic['rf_env'].reshape((-1,))
            dic['my_att'] = data_dic['my_att'][0][0]
        except:
            dic['rf_data'] = data_dic['rf_data'].reshape((-1,))
            dic['my_att'] = data_dic['my_att'].item()
            dic['rf_env'] = 0
        return dic['rf_data'] , dic['rf_env'], dic['my_att']


    # 2. Define Ultrasound pipeline:
    @sb.utils.data_pipeline.takes("rf_data")
    @sb.utils.data_pipeline.provides("sig","att")
    def ultrasound_pipeline(rf_data):
        rf_data, _, att = load_ultrasound(rf_data)
        len_wav = rf_data.shape[0]
        pddd = 4500#4000

        if len_wav < pddd:
            pad = np.zeros(pddd - len_wav)
            f_data = np.hstack([rf_data, pad])
        elif len_wav > pddd:
            rf_data = rf_data[:pddd]

        sig = rf_data
        #print('SIGNAL CALLINg from ultrasound pipline ',sig, att)
        yield sig
        yield att

    sb.dataio.dataset.add_dynamic_item(datasets, ultrasound_pipeline)

    sb.dataio.dataset.set_output_keys(
        datasets, ["sig", "att",],)

    #print(valid_data[0])
    
    return (
        train_data,
        valid_data,
        test_data,
    )





if __name__ == "__main__":

    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])

    print(hparams_file, run_opts, overrides)

    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)
        print(hparams)
    
    sb.create_experiment_directory(
    experiment_directory=hparams["output_folder"],
    hyperparams_to_save=hparams_file,
    overrides=overrides,
    )

    train_data,  valid_data, test_data = dataio_prepare(hparams)
    
    Ultra_brain = Ultra_Brain(
    modules=hparams["modules"],
    opt_class=hparams["optim"],
    hparams=hparams,
    run_opts=run_opts,
    checkpointer=hparams["checkpointer"],
    )

    Ultra_brain.fit(
    Ultra_brain.hparams.epoch_counter,
    train_data,
    valid_data,
    train_loader_kwargs=hparams["train_dataloader_opts"],
    valid_loader_kwargs=hparams["valid_dataloader_opts"],
    )

    test_stats = Ultra_brain.evaluate(
        test_set=test_data,
        test_loader_kwargs=hparams["test_dataloader_opts"],
        progressbar = True
    )