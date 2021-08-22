import glob
import logging
import os
import time
import torch
from typing import List

import librosa
import numpy as np
import pysepm
import pytorch_lightning as pl
import torch.nn as nn
from pesq import pesq
from pytorch_lightning.utilities import rank_zero_only

from bytesep.callbacks.base_callbacks import SaveCheckpointsCallback
from bytesep.inference import Separator
from bytesep.utils import StatisticsContainer, read_yaml, calculate_sdr


def get_violin_piano_callbacks(
    config_yaml: str,
    dataset_dir: str,
    workspace: str,
    checkpoints_dir: str,
    statistics_path: str,
    logger: pl.loggers.TensorBoardLogger,
    model: nn.Module,
    evaluate_device: str,
) -> List[pl.Callback]:
    """Get Voicebank-Demand callbacks of a config yaml.

    Args:
        config_yaml: str
        dataset_dir: str
        workspace: str
        checkpoints_dir: str
        statistics_dir: str
        logger: pl.loggers.TensorBoardLogger
        model: nn.Module
        evaluate_device: str

    Return:
        callbacks: List[pl.Callback]
    """
    configs = read_yaml(config_yaml)
    target_source_types = configs['train']['target_source_types']
    input_channels = configs['train']['channels']
    mono = True if input_channels == 1 else False
    # clean_dir = os.path.join(dataset_dir, configs['evaluate']['test']['clean_dir'])
    # noisy_dir = os.path.join(dataset_dir, configs['evaluate']['test']['noisy_dir'])
    evaluation_audios_dir = os.path.join(workspace, configs['evaluate']['test']['evaluation_audios_dir'])
    sample_rate = configs['train']['sample_rate']
    evaluate_step_frequency = configs['train']['evaluate_step_frequency']
    save_step_frequency = configs['train']['save_step_frequency']
    test_batch_size = configs['evaluate']['batch_size']
    test_segment_seconds = configs['evaluate']['segment_seconds']

    test_segment_samples = int(test_segment_seconds * sample_rate)
    assert len(target_source_types) == 1
    target_source_type = target_source_types[0]
    # assert target_source_type == 'speech'

    # save checkpoint callback
    save_checkpoints_callback = SaveCheckpointsCallback(
        model=model,
        checkpoints_dir=checkpoints_dir,
        save_step_frequency=save_step_frequency,
    )

    # statistics container
    statistics_container = StatisticsContainer(statistics_path)

    # evaluation callback
    evaluate_test_callback = EvaluationCallback(
        model=model,
        target_source_type=target_source_type,
        input_channels=input_channels,
        sample_rate=sample_rate,
        mono=mono,
        evaluation_audios_dir=evaluation_audios_dir,
        segment_samples=test_segment_samples,
        batch_size=test_batch_size,
        device=evaluate_device,
        evaluate_step_frequency=evaluate_step_frequency,
        logger=logger,
        statistics_container=statistics_container,
    )

    callbacks = [save_checkpoints_callback, evaluate_test_callback]

    return callbacks





class EvaluationCallback(pl.Callback):
    def __init__(
        self,
        model: nn.Module,
        input_channels,
        evaluation_audios_dir,
        target_source_type,
        sample_rate,
        mono,
        segment_samples: int,
        batch_size: int,
        device: str,
        evaluate_step_frequency: int,
        logger,
        statistics_container: StatisticsContainer,
    ):
        r"""Callback to evaluate every #save_step_frequency steps.

        Args:
            model: nn.Module
            input_channels: int
            clean_dir: str
            noisy_dir: str
            sample_rate: int
            segment_samples: int, length of segments to be input to a model, e.g., 44100*30
            batch_size, int, e.g., 12
            device: str, e.g., 'cuda'
            evaluate_step_frequency: int, evaluate every #save_step_frequency steps
            logger: object
            statistics_container: StatisticsContainer
        """
        self.model = model
        self.target_source_type = target_source_type
        self.sample_rate = sample_rate
        self.mono = mono
        self.segment_samples = segment_samples
        self.evaluate_step_frequency = evaluate_step_frequency
        self.logger = logger
        self.statistics_container = statistics_container

        self.evaluation_audios_dir = evaluation_audios_dir

        # separator
        self.separator = Separator(model, self.segment_samples, batch_size, device)

    
    @rank_zero_only
    def on_batch_end(self, trainer: pl.Trainer, _) -> None:
        r"""Evaluate losses on a few mini-batches. Losses are only used for
        observing training, and are not final F1 metrics.
        """

        global_step = trainer.global_step

        if global_step % self.evaluate_step_frequency == 0:

            # violin_audios_dir = os.path.join(self.evaluation_audios_dir, 'violin')
            # piano_audios_dir = os.path.join(self.evaluation_audios_dir, 'piano')
            mixture_audios_dir = os.path.join(self.evaluation_audios_dir, 'mixture')
            clean_audios_dir = os.path.join(self.evaluation_audios_dir, self.target_source_type)
            
            audio_names = sorted(os.listdir(mixture_audios_dir))

            error_str = "Directory {} does not contain audios for evaluation!".format(self.evaluation_audios_dir)
            assert len(audio_names) > 0, error_str

            logging.info("--- Step {} ---".format(global_step))
            logging.info("Total {} pieces for evaluation:".format(len(audio_names)))

            eval_time = time.time()

            sdrs = []

            for n, audio_name in enumerate(audio_names):

                # Load audio.
                mixture_path = os.path.join(mixture_audios_dir, audio_name)
                clean_path = os.path.join(clean_audios_dir, audio_name)
                
                mixture, _ = librosa.core.load(
                    mixture_path, sr=self.sample_rate, mono=self.mono
                )

                # Target
                clean, _ = librosa.core.load(
                    clean_path, sr=self.sample_rate, mono=self.mono
                )

                if mixture.ndim == 1:
                    mixture = mixture[None, :]
                # (channels, audio_length)

                input_dict = {'waveform': mixture}

                # from IPython import embed; embed(using=False); os._exit(0)
                # import soundfile
                # soundfile.write(file='_zz.wav', data=mixture[0], samplerate=self.sample_rate)

                # separate
                sep_wav = self.separator.separate(input_dict)
                # (channels, audio_length)

                sdr = calculate_sdr(ref=clean, est=sep_wav)

                print("{} SDR: {:.3f}".format(audio_name, sdr))
                sdrs.append(sdr)


            logging.info("-----------------------------")
            logging.info('Avg SDR: {:.3f}'.format(np.mean(sdrs)))
            
            logging.info("Evlauation time: {:.3f}".format(time.time() - eval_time))

            statistics = {"pesq": np.mean(sdrs)}
            self.statistics_container.append(global_step, statistics, 'test')
            self.statistics_container.dump()