import os
import sys
import shutil
from tqdm import tqdm
from pathlib import Path
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.distributed as dist
import torch.multiprocessing as mp

from datasets import get_loaders
from models import SimpleNet

from batteries import (
    seed_all,
    CheckpointManager,
    TensorboardLogger,
    t2d,
    make_checkpoint,
)


def setup(rank, world_size):
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "12355"

    # initialize the process group
    dist.init_process_group("gloo", rank=rank, world_size=world_size)


def cleanup():
    dist.destroy_process_group()


def train_fn(
    model,
    loader,
    device,
    loss_fn,
    optimizer,
    scheduler=None,
    accum_steps: int = 1,
    verbose=True,
):
    model.train()

    losses = []
    dataset_true_lbl = []
    dataset_pred_lbl = []

    to_iter = enumerate(loader)
    if device == 0:
        prbar = tqdm(to_iter, total=len(loader), file=sys.stdout, desc="train")
    else:
        prbar = to_iter
    for _idx, (bx, by) in prbar:
        bx, by = t2d((bx, by), device)

        optimizer.zero_grad()

        if isinstance(bx, (tuple, list)):
            outputs = model(*bx)
        elif isinstance(bx, dict):
            outputs = model(**bx)
        else:
            outputs = model(bx)

        loss = loss_fn(outputs, by)
        _loss = loss.item()
        losses.append(_loss)
        loss.backward()

        dataset_true_lbl.append(by.flatten().detach().cpu().numpy())
        dataset_pred_lbl.append(outputs.argmax(1).flatten().detach().cpu().numpy())

        if device == 0:
            prbar.set_postfix_str(f"loss {_loss:.4f}")

        if (_idx + 1) % accum_steps == 0:
            optimizer.step()
            if scheduler is not None:
                scheduler.step()

    dataset_true_lbl = np.concatenate(dataset_true_lbl)
    dataset_pred_lbl = np.concatenate(dataset_pred_lbl)
    dataset_acc = (dataset_true_lbl == dataset_pred_lbl).astype(float).mean()

    return np.mean(losses), dataset_acc


def valid_fn(model, loader, device, loss_fn):
    model.eval()

    losses = []
    dataset_true_lbl = []
    dataset_pred_lbl = []
    with torch.no_grad():
        to_iter = loader
        if device == 0:
            prbar = tqdm(loader, file=sys.stdout, desc="valid")
        else:
            prbar = to_iter
        for bx, by in prbar:
            bx, by = t2d((bx, by), device)

            if isinstance(bx, (tuple, list)):
                outputs = model(*bx)
            elif isinstance(bx, dict):
                outputs = model(**bx)
            else:
                outputs = model(bx)

            loss = loss_fn(outputs, by).item()
            losses.append(loss)

            if device == 0:
                prbar.set_postfix_str(f"loss {loss:.4f}")

            dataset_true_lbl.append(by.flatten().detach().cpu().numpy())
            dataset_pred_lbl.append(outputs.argmax(1).flatten().detach().cpu().numpy())

    dataset_true_lbl = np.concatenate(dataset_true_lbl)
    dataset_pred_lbl = np.concatenate(dataset_pred_lbl)
    dataset_acc = (dataset_true_lbl == dataset_pred_lbl).astype(float).mean()

    return np.mean(losses), dataset_acc


def experiment(rank, world_size, logdir: str):
    setup(rank, world_size)
    tb_logdir = logdir / "tensorboard"

    checkpointer = CheckpointManager(
        logdir=logdir, metric="accuracy", metric_minimization=False, save_n_best=3
    )

    seed_all()
    model = SimpleNet()
    model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
    model = model.to(rank)
    model = nn.parallel.DistributedDataParallel(model, device_ids=[rank])
    optimizer = optim.AdamW(model.parameters(), lr=1e-3)
    criterion = nn.CrossEntropyLoss()

    train_loader, valid_loader = get_loaders("", rank, world_size)

    with TensorboardLogger(tb_logdir) as tb:
        stage = "stage0"
        for ep in range(1, 10 + 1):
            if rank == 0:
                print(f"Epoch {ep}:")
            train_loss, train_acc = train_fn(
                model, train_loader, rank, criterion, optimizer
            )
            valid_loss, valid_acc = valid_fn(model, valid_loader, rank, criterion)

            if rank == 0:
                # log metrics
                tb.metric(
                    f"{stage}/loss", {"train": train_loss, "valid": valid_loss}, ep
                )
                tb.metric(
                    f"{stage}/accuracy", {"train": train_acc, "valid": valid_acc}, ep,
                )

                epoch_metrics = {
                    "train_loss": train_loss,
                    "train_accuracy": train_acc,
                    "valid_loss": valid_loss,
                    "valid_accuracy": valid_acc,
                }

                # store checkpoints
                checkpointer.process(
                    metric_value=valid_acc,
                    epoch=ep,
                    checkpoint=make_checkpoint(
                        stage, ep, model, optimizer, metrics=epoch_metrics,
                    ),
                )

                print(f"            train loss - {train_loss:.5f}")
                print(f"train dataset accuracy - {train_acc:.5f}")
                print(f"            valid loss - {valid_loss:.5f}")
                print(f"valid dataset accuracy - {valid_acc:.5f}")
                print()

    cleanup()


def main() -> None:
    # do some pre cleaning & stuff
    logdir = Path(".") / "logs" / "ddp-experiment"
    device = torch.device("cuda:0")

    if os.path.isdir(logdir):
        shutil.rmtree(logdir, ignore_errors=True)
        print(f"* Removed existing '{logdir}' directory!")

    world_size = torch.cuda.device_count()

    mp.spawn(experiment, args=(world_size, logdir,), nprocs=world_size, join=True)

    # do some post cleaning


if __name__ == "__main__":
    main()
