import os
import sys
import shutil
from pathlib import Path
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim

from datasets import get_loaders
from models import SimpleNet

from batteries import (
    seed_all,
    CheckpointManager,
    TensorboardLogger,
    t2d,
    make_checkpoint,
    load_checkpoint,
)
from batteries.progress import tqdm


######################################################################
# TODOs:
# 1. save tensorboard metrics after each step (train/valid)
# 2. typings and docs to each function
######################################################################


def train_fn(
    model,
    loader,
    device,
    loss_fn,
    optimizer,
    scheduler=None,
    accum_steps: int = 1,
    verbose: bool = True,
):
    model.train()

    losses = []
    dataset_true_lbl = []
    dataset_pred_lbl = []

    with tqdm(total=len(loader), desc="train", disable=not verbose) as progress:
        for _idx, (bx, by) in enumerate(loader):
            bx, by = t2d((bx, by), device)
            # by = t2d(by, device)

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

            progress.set_postfix_str(f"loss {_loss:.4f}")

            if (_idx + 1) % accum_steps == 0:
                optimizer.step()
                if scheduler is not None:
                    scheduler.step()

            progress.update(1)

    dataset_true_lbl = np.concatenate(dataset_true_lbl)
    dataset_pred_lbl = np.concatenate(dataset_pred_lbl)
    dataset_acc = (dataset_true_lbl == dataset_pred_lbl).astype(float).mean()

    return np.mean(losses), dataset_acc


def valid_fn(model, loader, device, loss_fn, verbose: bool = True):
    model.eval()

    losses = []
    dataset_true_lbl = []
    dataset_pred_lbl = []
    with torch.no_grad(), tqdm(
        total=len(loader), desc="valid", disable=not verbose
    ) as progress:
        for bx, by in loader:
            bx, by = t2d((bx, by), device)

            if isinstance(bx, (tuple, list)):
                outputs = model(*bx)
            elif isinstance(bx, dict):
                outputs = model(**bx)
            else:
                outputs = model(bx)

            loss = loss_fn(outputs, by).item()
            losses.append(loss)

            progress.set_postfix_str(f"loss {loss:.4f}")

            dataset_true_lbl.append(by.flatten().detach().cpu().numpy())
            dataset_pred_lbl.append(outputs.argmax(1).flatten().detach().cpu().numpy())

            progress.update(1)

    dataset_true_lbl = np.concatenate(dataset_true_lbl)
    dataset_pred_lbl = np.concatenate(dataset_pred_lbl)
    dataset_acc = (dataset_true_lbl == dataset_pred_lbl).astype(float).mean()

    return np.mean(losses), dataset_acc


def experiment(logdir: str, device: str):
    tb_logdir = logdir / "tensorboard"

    seed_all()
    model = SimpleNet().to(device)
    optimizer = optim.AdamW(model.parameters(), lr=1e-3)
    criterion = nn.CrossEntropyLoss()

    train_loader, valid_loader = get_loaders("")

    with TensorboardLogger(tb_logdir) as tb:
        stage = "stage0"
        n_epochs = 10

        checkpointer = CheckpointManager(
            logdir=logdir / stage,
            metric="accuracy",
            metric_minimization=False,
            save_n_best=3,
        )

        for ep in range(1, n_epochs + 1):
            print(f"[Epoch {ep}/{n_epochs}]")
            train_loss, train_acc = train_fn(
                model, train_loader, device, criterion, optimizer
            )
            valid_loss, valid_acc = valid_fn(model, valid_loader, device, criterion)

            # log metrics
            tb.metric(f"{stage}/loss", {"train": train_loss, "valid": valid_loss}, ep)
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
                score=valid_acc,
                epoch=ep,
                checkpoint=make_checkpoint(
                    stage, ep, model, optimizer, metrics=epoch_metrics,
                ),
            )

            print()
            print(f"            train loss - {train_loss:.5f}")
            print(f"train dataset accuracy - {train_acc:.5f}")
            print(f"            valid loss - {valid_loss:.5f}")
            print(f"valid dataset accuracy - {valid_acc:.5f}")
            print()

        # do a next training stage
        stage = "stage1"
        n_epochs = 10
        print(f"\n\nStage - {stage}")

        checkpointer = CheckpointManager(
            logdir=logdir / stage,
            metric="accuracy",
            metric_minimization=False,
            save_n_best=3,
        )

        load_checkpoint(logdir / "stage0" / "best.pth", model)
        optimizer = optim.Adam(model.parameters(), lr=1e-4 / 2)

        for ep in range(1, n_epochs + 1):
            print(f"[Epoch {ep}/{n_epochs}]")
            train_loss, train_acc = train_fn(
                model, train_loader, device, criterion, optimizer
            )
            valid_loss, valid_acc = valid_fn(model, valid_loader, device, criterion)

            # log metrics
            tb.metric(f"{stage}/loss", {"train": train_loss, "valid": valid_loss}, ep)
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
                score=valid_acc,
                epoch=ep,
                checkpoint=make_checkpoint(
                    stage, ep, model, optimizer, metrics=epoch_metrics,
                ),
            )

            print()
            print(f"            train loss - {train_loss:.5f}")
            print(f"train dataset accuracy - {train_acc:.5f}")
            print(f"            valid loss - {valid_loss:.5f}")
            print(f"valid dataset accuracy - {valid_acc:.5f}")
            print()

        load_checkpoint(logdir / "stage1" / "best.pth", model)


def main() -> None:
    # do some pre cleaning & stuff
    logdir = Path(".") / "logs" / "single-device-experiment"
    device = torch.device("cuda:0")

    if os.path.isdir(logdir):
        shutil.rmtree(logdir, ignore_errors=True)
        print(f"* Removed existing '{logdir}' directory!")

    experiment(logdir, device)

    # do some post cleaning


if __name__ == "__main__":
    main()
