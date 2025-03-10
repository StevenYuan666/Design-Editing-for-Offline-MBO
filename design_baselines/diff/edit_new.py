import copy
import json
import os
import random
import string
import uuid
import shutil

from typing import Optional, Union
from pprint import pprint

import configargparse

import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout


@contextmanager
def suppress_output():
    """
        A context manager that redirects stdout and stderr to devnull
        https://stackoverflow.com/a/52442331
    """
    with open(os.devnull, 'w') as fnull:
        with redirect_stderr(fnull) as err, redirect_stdout(fnull) as out:
            yield (err, out)


with suppress_output():
    import design_bench

    from design_bench.datasets.discrete.tf_bind_8_dataset import TFBind8Dataset
    from design_bench.datasets.discrete.tf_bind_10_dataset import TFBind10Dataset
    from design_bench.datasets.discrete.nas_bench_dataset import NASBenchDataset
    from design_bench.datasets.discrete.chembl_dataset import ChEMBLDataset

    from design_bench.datasets.continuous.ant_morphology_dataset import AntMorphologyDataset
    from design_bench.datasets.continuous.dkitty_morphology_dataset import DKittyMorphologyDataset
    from design_bench.datasets.continuous.superconductor_dataset import SuperconductorDataset
    # from design_bench.datasets.continuous.hopper_controller_dataset import HopperControllerDataset

import numpy as np
import pandas as pd
import tensorflow as tf
import pytorch_lightning as pl
import pickle as pkl

import torch
from torch.utils.data import Dataset, DataLoader

from nets import DiffusionTest, DiffusionScore
from util import TASKNAME2TASK, configure_gpu, set_seed, get_weights
# from forward import ForwardModel

args_filename = "args.json"
checkpoint_dir = "checkpoints"
wandb_project = "sde-flow"


class RvSDataset(Dataset):

    def __init__(self, task, x, y, w=None, device=None, mode='train'):
        self.task = task
        self.device = device
        self.mode = mode
        self.x = x
        self.y = y
        self.w = w

    def __len__(self):
        return self.x.shape[0]

    def __getitem__(self, idx):
        x = torch.tensor(self.x[idx])
        y = torch.tensor(self.y[idx])
        if self.w is not None:
            w = torch.tensor(self.w[idx])
        else:
            w = None
        '''
        if self.device is not None:
            x = x.to(self.device)
            y = y.to(self.device)
            if w is not None:
                w = w.to(self.device)
        '''
        if w is None:
            return x, y
        else:
            return x, y, w


def temp_get_super_y(task):
    y = task.y.reshape(-1)
    sorted_y_idx = np.argsort(y)

    super_task = design_bench.make(TASKNAME2TASK["superconductor"])
    super_task.map_normalize_y()

    super_y = super_task.y.reshape(-1)
    super_y = np.sort(super_y)
    super_y = super_y[sorted_y_idx]

    return super_y


def split_dataset(task, val_frac=None, device=None, temp=None):
    length = task.y.shape[0]
    shuffle_idx = np.arange(length)
    shuffle_idx = np.random.shuffle(shuffle_idx)

    if task.is_discrete:
        task.map_to_logits()
        x = task.x[shuffle_idx]
        x = x.reshape(x.shape[1:])
        x = x.reshape(x.shape[0], -1)
    else:
        x = task.x[shuffle_idx]

    # y = temp_get_super_y(task)
    y = task.y
    y = y[shuffle_idx]
    if not task.is_discrete:
        x = x.reshape(-1, task.x.shape[-1])
    y = y.reshape(-1, 1)
    # w = get_weights(y, base_temp=0.03 * length)
    # w = get_weights(y, base_temp=0.1)
    w = get_weights(y, temp=temp)

    # TODO: Modify
    # full_ds = DKittyMorphologyDataset()
    # y = (y - full_ds.y.min()) / (full_ds.y.max() - full_ds.y.min())

    print(w)
    print(w.shape)

    if val_frac is None:
        val_frac = 0

    val_length = int(length * val_frac)
    train_length = length - val_length

    train_dataset = RvSDataset(
        task,
        x[:train_length],
        y[:train_length],
        # None,
        w[:train_length],
        device,
        mode='train')
    val_dataset = RvSDataset(
        task,
        x[train_length:],
        y[train_length:],
        # None,
        w[train_length:],
        device,
        mode='val')

    return train_dataset, val_dataset


def split_dataset_based_on_top_candidates(task, size, val_frac=None, device=None, temp=None):
    length = task.y.shape[0]
    shuffle_idx = np.arange(length)
    shuffle_idx = np.random.shuffle(shuffle_idx)

    if task.is_discrete:
        task.map_to_logits()
        x = task.x[shuffle_idx]
        x = x.reshape(x.shape[1:])
        x = x.reshape(x.shape[0], -1)
    else:
        x = task.x[shuffle_idx]

    # y = temp_get_super_y(task)
    y = task.y
    y = y[shuffle_idx]
    if not task.is_discrete:
        x = x.reshape(-1, task.x.shape[-1])
    y = y.reshape(-1, 1)
    w = get_weights(y, temp=temp)

    # Sort y in descending order and select the top 'size' instances
    sorted_indices = np.argsort(-y, axis=0).flatten()
    top_indices = sorted_indices[:size]
    x = x[top_indices]
    y = y[top_indices]
    w = w[top_indices]

    if val_frac is None:
        val_frac = 0

    val_length = int(length * val_frac)
    train_length = length - val_length

    train_dataset = RvSDataset(
        task,
        x[:train_length],
        y[:train_length],
        # None,
        w[:train_length],
        device,
        mode='train')
    val_dataset = RvSDataset(
        task,
        x[train_length:],
        y[train_length:],
        # None,
        w[train_length:],
        device,
        mode='val')

    return train_dataset, val_dataset


class RvSDataModule(pl.LightningDataModule):

    def __init__(self, task, batch_size, num_workers, val_frac, device, temp, top_candidates_size=None):
        super().__init__()

        self.task = task
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.val_frac = val_frac
        self.device = device
        self.train_dataset = None
        self.val_dataset = None
        self.temp = temp
        self.top_candidates_size = top_candidates_size

    def setup(self, stage=None):
        if self.top_candidates_size is not None:
            self.train_dataset, self.val_dataset = split_dataset_based_on_top_candidates(
                self.task, size=self.top_candidates_size, val_frac=self.val_frac, device=self.device, temp=self.temp)
        else:
            self.train_dataset, self.val_dataset = split_dataset(
                self.task, self.val_frac, self.device, self.temp)

    def train_dataloader(self):
        train_loader = DataLoader(self.train_dataset,
                                  num_workers=self.num_workers,
                                  batch_size=self.batch_size)
        return train_loader

    def val_dataloader(self):
        val_loader = DataLoader(self.val_dataset,
                                num_workers=self.num_workers,
                                batch_size=self.batch_size)
        return val_loader


def log_args(
        args: configargparse.Namespace,
        wandb_logger: pl.loggers.wandb.WandbLogger,
) -> None:
    """Log arguments to a file in the wandb directory."""
    wandb_logger.log_hyperparams(args)

    args.wandb_entity = wandb_logger.experiment.entity
    args.wandb_project = wandb_logger.experiment.project
    args.wandb_run_id = wandb_logger.experiment.id
    args.wandb_path = wandb_logger.experiment.path

    out_directory = wandb_logger.experiment.dir
    pprint(f"out_directory: {out_directory}")
    args_file = os.path.join(out_directory, args_filename)
    with open(args_file, "w") as f:
        try:
            json.dump(args.__dict__, f)
        except AttributeError:
            json.dump(args, f)


def run_evaluate(
    taskname,
    seed,
    hidden_size,
    learning_rate,
    source_checkpoint_path,
    target_checkpoint_path,
    args,
    wandb_logger=None,
    device=None,
    normalise_x=False,
    normalise_y=False,
):
    set_seed(seed)
    # task = design_bench.make(TASKNAME2TASK[taskname])

    if taskname != 'tf-bind-10':
        task = design_bench.make(TASKNAME2TASK[taskname])
    else:
        task = design_bench.make(TASKNAME2TASK[taskname],
                                 dataset_kwargs={"max_samples": 30000})

    if task.is_discrete:
        task.map_to_logits()
    if normalise_x:
        task.map_normalize_x()
    if normalise_y:
        task.map_normalize_y()

    if not args.score_matching:
        model = DiffusionTest.load_from_checkpoint(
            checkpoint_path=checkpoint_path,
            taskname=taskname,
            task=task,
            learning_rate=args.learning_rate,
            hidden_size=args.hidden_size,
            vtype=args.vtype,
            beta_min=args.beta_min,
            beta_max=args.beta_max,
            T0=args.T0,
            dropout_p=args.dropout_p)
    else:
        print("Score matching loss")
        model = DiffusionScore.load_from_checkpoint(
            checkpoint_path=source_checkpoint_path,
            taskname=taskname,
            task=task,
            learning_rate=args.learning_rate,
            hidden_size=args.hidden_size,
            vtype=args.vtype,
            beta_min=args.beta_min,
            beta_max=args.beta_max,
            T0=args.T0,
            dropout_p=args.dropout_p)

    target_model = DiffusionScore.load_from_checkpoint(
        checkpoint_path=checkpoint_path,
        taskname=taskname,
        task=task,
        learning_rate=args.learning_rate,
        hidden_size=args.hidden_size,
        vtype=args.vtype,
        beta_min=args.beta_min,
        beta_max=args.beta_max,
        T0=args.T0,
        dropout_p=args.dropout_p)

    model = model.to(device)
    model.eval()
    target_model = target_model.to(device)
    target_model.eval()

    def heun_sampler(sde, x_0, ya, num_steps, start_step=0, end_step=None, lmbd=0., keep_all_samples=True):
        device = sde.gen_sde.T.device
        batch_size = x_0.size(0)
        ndim = x_0.dim() - 1
        T_ = sde.gen_sde.T.cpu().item()
        delta = T_ / num_steps
        ts = torch.linspace(0, 1, num_steps + 1) * T_

        # sample
        xs = []
        x_t = x_0.detach().clone().to(device)
        t = torch.zeros(batch_size, *([1] * ndim), device=device)
        t_n = torch.zeros(batch_size, *([1] * ndim), device=device)

        if end_step is None:
            end_step = num_steps

        with torch.no_grad():
            for i in range(start_step, end_step):
                t.fill_(ts[i].item())
                if i < num_steps - 1:
                    t_n.fill_(ts[i + 1].item())
                mu = sde.gen_sde.mu(t, x_t, ya, lmbd=lmbd, gamma=args.gamma)
                sigma = sde.gen_sde.sigma(t, x_t, lmbd=lmbd)
                x_t = x_t + delta * mu + delta**0.5 * sigma * torch.randn_like(
                    x_t
                )  # one step update of Euler Maruyama method with a step size delta
                # Additional terms for Heun's method
                if i < num_steps - 1:
                    mu2 = sde.gen_sde.mu(t_n,
                                         x_t,
                                         ya,
                                         lmbd=lmbd,
                                         gamma=args.gamma)
                    sigma2 = sde.gen_sde.sigma(t_n, x_t, lmbd=lmbd)
                    x_t = x_t + (sigma2 -
                                 sigma) / 2 * delta**0.5 * torch.randn_like(x_t)

                if keep_all_samples or i == num_steps - 1:
                    xs.append(x_t.cpu())
                else:
                    pass
        return xs


    num_steps = args.num_steps
    num_samples = 256
    # num_samples = 10

    # lmbds = [0., 1.]
    lmbds = [args.lamda]

    # use the max of the dataset instead
    args.condition = task.y.max()

    @torch.no_grad()
    def _get_trained_model():
        checkpoint_path = f"experiments/{taskname}/forward_model/123/wandb/latest-run/files/checkpoints/last.ckpt"
        model = ForwardModel.load_from_checkpoint(
            checkpoint_path=checkpoint_path,
            taskname=taskname,
            task=task, )

        return model

    # sample and plot
    designs = []
    results = []
    for lmbd in lmbds:
        if not task.is_discrete:
            x_0 = torch.randn(num_samples, task.x.shape[-1],
                              device=device)  # init from prior
        else:
            x_0 = torch.randn(num_samples,
                              task.x.shape[-1] * task.x.shape[-2],
                              device=device)  # init from prior

        print(x_0.shape)

        # Generate a sample from the source distribution
        y_ = torch.ones(num_samples).to(device) * args.condition

        if not args.edit:
            print("using source ddom...")
            xs_base = heun_sampler(model,
                                   x_0,
                                   y_,
                                   num_steps,
                                   start_step=0,
                                   end_step=1000,
                                   lmbd=lmbd,
                                   keep_all_samples=True)
            xs_base = [xs_base[-1].to(device)]
        else:
            print("using offline dataset...")
            task_x = copy.deepcopy(task.x)
            task_y = copy.deepcopy(task.y)
            if task.is_discrete:
                task_x = task_x.reshape(task_x.shape[0], -1)
            task_x = torch.Tensor(task_x).to(device)
            task_y = torch.Tensor(task_y).to(device)
            index = torch.argsort(-task_y.squeeze())
            index = index[:num_samples]
            x_0 = copy.deepcopy(task_x[index])
            xs_base = [torch.asarray(x_0, device=device)]

        # Editing towards the target distribution
        xs_base = xs_base[-1]
        # This is a hyperparameter to trade off between the source distribution and the target distribution
        # See SDEdit paper for more details

        t_prop = args.t

        t_ = torch.tensor([t_prop]).expand(num_samples).view(-1, 1).to(device)
        # x_hat, target, std, g = model.gen_sde.base_sde.sample(t_, xs_base, return_noise=True)  # Add noise
        # xs = [x_hat]
        # Denoise again

        target_xy = np.load(f"experiments/{taskname}/{TASKNAME2TASK[taskname]}_pseudo_target_123.npy", allow_pickle=True).item()
        target_x = np.array(target_xy["x"])
        target_y = np.array(target_xy["pred_y"])[:, np.newaxis]
        y_max = np.max(target_y)
        y_mean = np.mean(target_y)
        y_std = np.std(target_y)
        print(y_[0])
        print(y_max, y_mean, y_std)
        y_ = torch.ones(num_samples).to(device) * 1.5

        xs_base = [torch.asarray(target_x[:num_samples], device=device)]
        xs_base = xs_base[-1]
        x_hat, target, std, g = model.gen_sde.base_sde.sample(t_, xs_base, return_noise=True)  # Add noise

        xs = heun_sampler(target_model,
                          x_hat,
                          y_,
                          num_steps,
                          start_step=int(1000 * (1 - t_prop)),
                          end_step=1000,
                          lmbd=lmbd,
                          keep_all_samples=True)
        if not args.edit:
            xs = [xs_base]

        ctr = 0
        # pred_model = _get_trained_model()
        # preds = []
        for qqq in [xs[-1]]:
            ctr += 1
            print(qqq.shape)
            if not qqq.isnan().any():
                designs.append(qqq.cpu().numpy())

                if not task.is_discrete:
                    ys = task.predict(qqq.cpu().numpy())
                else:
                    qqq = qqq.view(qqq.size(0), -1, task.x.shape[-1])
                    ys = task.predict(qqq.cpu().numpy())

                # pred_ys = pred_model.mlp(qqq)
                # preds.append(pred_ys.cpu().numpy())

                print("GT ys: {}".format(ys.max()))
                # print("Pred ys: {}".format(pred_ys.max()))
                if normalise_y:
                    print("normalise")
                    prop_v = (ys > task.y.max()).mean()
                    print(prop_v)
                    ys = task.denormalize_y(ys)
                else:
                    print("none")
                dic2y = np.load("npy/dic2y.npy", allow_pickle=True).item()
                y_min, y_max = dic2y[TASKNAME2TASK[taskname]]
                max_v = (np.max(ys) - y_min) / (y_max - y_min)
                med_v = (np.median(ys) - y_min) / (y_max - y_min)
                print("Max Score: ", max_v)
                print("Median Score: ", med_v)
                results.append(ys)

                if not os.path.exists(f"results/{taskname}"):
                    os.makedirs(f"results/{taskname}")

                with open(f"results/{taskname}/{args.save_prefix}_{seed}.json", "w") as f:
                    json.dump({
                        "max": float(max_v),
                        "med": float(med_v),
                        "prop": float(prop_v)
                    }, f)

            else:
                print("fuck")

    designs = np.concatenate(designs, axis=0)
    results = np.concatenate(results, axis=0)


if __name__ == "__main__":
    parser = configargparse.ArgumentParser()
    # configuration
    parser.add_argument(
        "--configs",
        default=None,
        required=False,
        is_config_file=True,
        help="path(s) to configuration file(s)",
    )
    parser.add_argument('--mode',
                        choices=['train', 'eval'],
                        default='train',
                        )
    parser.add_argument('--task',
                        choices=list(TASKNAME2TASK.keys()),
                        default='ant',
                        )
    # reproducibility
    parser.add_argument(
        "--seed",
        default=0,
        type=int,
        help=
        "sets the random seed; if this is not specified, it is chosen randomly",
    )
    parser.add_argument("--condition", default=0.0, type=float)
    parser.add_argument("--lamda", default=0.0, type=float)
    parser.add_argument("--temp", default='90', type=str)
    parser.add_argument("--suffix", type=str, default="")
    # experiment tracking
    parser.add_argument("--name", type=str, help="Experiment name")
    parser.add_argument("--score_matching", action='store_true', default=False)
    # training
    train_time_group = parser.add_mutually_exclusive_group(required=False)
    train_time_group.add_argument(
        "--epochs",
        default=200,
        type=int,
        help="the number of training epochs.",
    )
    train_time_group.add_argument(
        "--max_steps",
        default=None,
        type=int,
        help=
        "the number of training gradient steps per bootstrap iteration. ignored "
        "if --train_time is set",
    )
    train_time_group.add_argument(
        "--train_time",
        default="00:01:00:00",
        type=str,
        help="how long to train, specified as a DD:HH:MM:SS str",
    )
    parser.add_argument("--num_workers",
                        default=1,
                        type=int,
                        help="Number of workers")
    checkpoint_frequency_group = parser.add_mutually_exclusive_group(
        required=False)
    checkpoint_frequency_group.add_argument(
        "--checkpoint_every_n_epochs",
        type=int,
        help="the period of training epochs for saving checkpoints",
    )
    checkpoint_frequency_group.add_argument(
        "--checkpoint_every_n_steps",
        type=int,
        help="the period of training gradient steps for saving checkpoints",
    )
    checkpoint_frequency_group.add_argument(
        "--checkpoint_time_interval",
        type=str,
        help="how long between saving checkpoints, specified as a HH:MM:SS str",
    )
    parser.add_argument(
        "--val_frac",
        type=float,
        default=0.1,
        help="fraction of data to use for validation",
    )
    parser.add_argument(
        "--use_gpu",
        action="store_true",
        default=True,
        help="place networks and data on the GPU",
    )
    parser.add_argument('--simple_clip', action="store_true", default=False)
    parser.add_argument("--which_gpu",
                        default=0,
                        type=int,
                        help="which GPU to use")
    parser.add_argument(
        "--normalise_x",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--normalise_y",
        action="store_true",
        default=False,
    )

    # i/o
    parser.add_argument('--dataset',
                        type=str,
                        choices=['mnist', 'cifar'],
                        default='mnist')
    parser.add_argument('--dataroot', type=str, default='~/.datasets')
    parser.add_argument('--saveroot', type=str, default='~/.saved')
    parser.add_argument('--expname', type=str, default='default')
    parser.add_argument('--num_steps',
                        type=int,
                        default=1000,
                        help='number of integration steps for sampling')

    # optimization
    parser.add_argument('--T0',
                        type=float,
                        default=1.0,
                        help='integration time')
    parser.add_argument('--vtype',
                        type=str,
                        choices=['rademacher', 'gaussian'],
                        default='rademacher',
                        help='random vector for the Hutchinson trace estimator')
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--test_batch_size', type=int, default=256)
    parser.add_argument('--num_iterations', type=int, default=10000)
    parser.add_argument('--gamma', type=float, default=1.)

    # model
    parser.add_argument(
        '--real',
        type=eval,
        choices=[True, False],
        default=True,
        help=
        'transforming the data from [0,1] to the real space using the logit function'
    )
    parser.add_argument(
        '--debias',
        action="store_true",
        default=False,
        help=
        'using non-uniform sampling to debias the denoising score matching loss'
    )

    # TODO: remove
    parser.add_argument(
        "--learning_rate",
        type=float,
        required=False,
        help="learning rate for each gradient step",
    )
    parser.add_argument(
        "--auto_tune_lr",
        action="store_true",
        default=False,
        help=
        "have PyTorch Lightning try to automatically find the best learning rate",
    )
    parser.add_argument(
        "--hidden_size",
        type=int,
        required=False,
        help="size of hidden layers in policy network",
    )
    parser.add_argument(
        "--depth",
        type=int,
        required=False,
        help="number of hidden layers in policy network",
    )
    parser.add_argument(
        "--dropout_p",
        type=float,
        required=False,
        help="dropout probability",
        default=0,
    )
    parser.add_argument(
        "--beta_min",
        type=float,
        required=False,
        default=0.1,
    )
    parser.add_argument(
        "--beta_max",
        type=float,
        required=False,
        default=20.0,
    )
    parser.add_argument(
        "--top_candidates_size",
        type=int,
        required=False,
        default=None,
    )
    parser.add_argument(
        "--target_checkpoint_path",
        type=str,
        required=False,
        help="Path to the target model checkpoint",
    )
    parser.add_argument(
        "--save_prefix",
        type=str,
        required=False,
        default="unknown",
    )
    parser.add_argument(
        "--edit",
        type=eval,
        choices=[True, False],
        default=False,
    )
    parser.add_argument(
        "--t",
        type=float,
        default=0.4,
        required=False,
    )
    args = parser.parse_args()

    wandb_project = "score-matching " if args.score_matching else "sde-flow"

    args.seed = np.random.randint(2 ** 31 - 1) if args.seed is None else args.seed
    set_seed(args.seed + 1)
    device = configure_gpu(args.use_gpu, args.which_gpu)

    expt_save_path = f"./experiments/{args.task}/{args.name}/{args.seed}"

    if args.mode == 'eval':
        if args.task == "superconductor":
            checkpoint_path = os.path.join(
                "experiments/superconductor/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
            args.target_checkpoint_path = os.path.join(
                "experiments/superconductor/score_diffusion/123/wandb/target_grad_pred/files/checkpoints/last.ckpt")
        elif args.task == "tf-bind-8":
            checkpoint_path = os.path.join(
                "experiments/tf-bind-8/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
            args.target_checkpoint_path = os.path.join(
                "experiments/tf-bind-8/score_diffusion/123/wandb/target_grad_gt/files/checkpoints/last.ckpt")
        elif args.task == "tf-bind-10":
            checkpoint_path = os.path.join(
                "experiments/tf-bind-10/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
            args.target_checkpoint_path = os.path.join(
                "experiments/tf-bind-10/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
        elif args.task == "dkitty":
            checkpoint_path = os.path.join(
                "experiments/dkitty/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
            args.target_checkpoint_path = os.path.join(
                "experiments/dkitty/score_diffusion/123/wandb/target_grad_pred/files/checkpoints/last.ckpt")
        elif args.task == "ant":
            checkpoint_path = os.path.join(
                "experiments/ant/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
            args.target_checkpoint_path = os.path.join(
                "experiments/ant/score_diffusion/123/wandb/target_grad_pred/files/checkpoints/last.ckpt")
        elif args.task == "nas":
            checkpoint_path = os.path.join(
                "experiments/nas/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
            args.target_checkpoint_path = os.path.join(
                "experiments/nas/score_diffusion/123/wandb/target_grad_pred_1e-2/files/checkpoints/last.ckpt")
        elif args.task == "hopper":
            checkpoint_path = os.path.join(
                "experiments/hopper/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
            args.target_checkpoint_path = os.path.join(
                "experiments/hopper/score_diffusion/123/wandb/source/files/checkpoints/last.ckpt")
        run_evaluate(taskname=args.task,
                     seed=args.seed,
                     hidden_size=args.hidden_size,
                     args=args,
                     learning_rate=args.learning_rate,
                     source_checkpoint_path=checkpoint_path,
                     target_checkpoint_path=args.target_checkpoint_path,
                     device=device,
                     normalise_x=args.normalise_x,
                     normalise_y=args.normalise_y)
    else:
        raise NotImplementedError
