import os.path

import torch
import torch.optim
from torch import nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard.writer import SummaryWriter
from tqdm import tqdm

from src import cnn, mask
from src.dataset import DATASET_MODES, SpikesDataset


def model_step(net, criterion, masker, batch, device, masking=True):
    X, rate, _, _ = batch

    rate = rate.to(device)
    if masking:
        the_mask = masker(X)
    else:
        the_mask = torch.zeros_like(X)

    assert X.shape == the_mask.shape
    assert the_mask.sum() < 0.5 * the_mask.numel()

    # Replace masked values with 0
    X_masked = X * (1 - the_mask.to(torch.float32))
    assert X_masked.sum() <= X.sum()

    X_smoothed = net((X_masked).to(dtype=torch.float32, device=device))
    loss = criterion(
        X_smoothed[the_mask].to(dtype=torch.float32, device=device), X[the_mask].to(dtype=torch.float32, device=device)
    )
    return loss, X_smoothed, X, the_mask, rate


def log_metrics(preds, targets, mask, logger, prefix, epoch):
    assert targets.min() >= 0, "Negative targets"

    logger.add_scalar(f"{prefix}/loss", loss, epoch)
    logger.add_scalar(f"{prefix}/mean_preds", preds.mean(), epoch)
    logger.add_scalar(f"{prefix}/std_preds", preds.std(), epoch)
    logger.add_scalar(f"{prefix}/mean_targets", targets.to(torch.float32).mean(), epoch)
    logger.add_scalar(f"{prefix}/std_targets", targets.to(torch.float32).std(), epoch)
    logger.add_scalar(f"{prefix}/mean_mask", mask.to(torch.float32).mean(), epoch)


if __name__ == "__main__":
    import sys
    data_source = "../data/config/lorenz.yaml"
    num_epochs = 100  # or the number of epochs you want to train for
    learning_rate = float(sys.argv[1])  # or the learning rate you want to use
    print('Training on learning rate ' + str(learning_rate))

    # Instantiate your model here
    net = cnn.CNN(29, 10)

    logger = SummaryWriter('Learning_rate='+str(learning_rate))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # M1 Mac-specific
    if device == torch.device("cpu") and torch.backends.mps.is_available():
        device = torch.device("mps")
    # device = torch.device("cpu")

    net = net.to(device)
    # criterion = nn.PoissonNLLLoss(log_input=True)
    criterion = nn.MSELoss(reduce=True)
    masker = mask.Masker()
    train_dataset = SpikesDataset(data_source)
    val_dataset = SpikesDataset(data_source, DATASET_MODES.val)
    train_loader = DataLoader(train_dataset, batch_size=64, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=64, shuffle=False)

    optimizer = torch.optim.Adam(net.parameters(), lr=learning_rate, amsgrad=True)

    for epoch in tqdm(range(num_epochs), desc="Epochs"):
        net.train()

        # Train loop
        for batch_num, batch in tqdm(enumerate(train_loader), desc="Train Batch"):
            optimizer.zero_grad()
            loss, preds, targets, the_mask, rate = model_step(
                net, criterion, masker, batch, device
            )
            loss.backward()
            optimizer.step()

            total_epoch = epoch * len(train_loader) + batch_num
            log_metrics(preds, targets, the_mask, logger, "train", total_epoch)
        
        # At end of train loop, log some more
        logger.add_image("train/preds", preds[-1], dataformats='HW', global_step=epoch)
        logger.add_image("train/targets", targets[-1], dataformats='HW', global_step=epoch)
        logger.add_image("train/the_mask", the_mask[-1], dataformats='HW', global_step=epoch)
        logger.add_image("train/rate", rate[-1], dataformats='HW', global_step=epoch)

        # Validate loop
        net.eval()
        with torch.no_grad():
            for batch_num, batch in tqdm(enumerate(val_loader), desc="Val Batch"):
                total_epoch = epoch * len(val_loader) + batch_num
                loss, preds, targets, the_mask, rate = model_step(
                    net, criterion, masker, batch, device, False
                )
                log_metrics(preds, targets, the_mask, logger, "val", total_epoch)

                if rate is not None:
                    _, preds, targets, the_mask, rate = model_step(
                        net, criterion, masker, batch, device, False
                    )
                    r2 = (
                        torch.corrcoef(torch.stack([preds.ravel(), rate.ravel()]))[0, 1]
                        ** 2
                    )
                    logger.add_scalar("val/r2", r2, total_epoch)

        # At end of val loop, log some more
        logger.add_image("val/preds", preds[-1], dataformats='HW', global_step=epoch)
        logger.add_image("val/targets", targets[-1], dataformats='HW', global_step=epoch)
        logger.add_image("val/the_mask", the_mask[-1], dataformats='HW', global_step=epoch)
        logger.add_image("val/rate", rate[-1], dataformats='HW', global_step=epoch)

        # Save model
        torch.save(net.state_dict(), os.path.join(logger.get_logdir(), f"model.pt"))
