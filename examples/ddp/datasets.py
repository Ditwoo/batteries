from torch.utils.data import DataLoader, Subset
from torch.utils.data.distributed import DistributedSampler

from torchvision.datasets import MNIST
from torchvision.transforms import Compose, Normalize, ToTensor


def get_transforms(dataset: str):
    """Get transforms depends from dataset.

    Args:
        dataset (str): dataset type (train or valid)

    Returns:
        dataset transforms
    """
    return Compose([ToTensor(), Normalize((0.1307,), (0.3081,))])


def get_loaders(stage: str, rank: int, world_size: int) -> tuple:
    """Loaders for a stage.

    Args:
        stage (str): stage name

    Returns:
        training and validation loader
    """
    trainset = MNIST(
        "./data", train=False, download=True, transform=get_transforms("train"),
    )
    train_sampler = DistributedSampler(trainset, num_replicas=world_size, rank=rank)
    testset = MNIST(
        "./data", train=False, download=True, transform=get_transforms("valid"),
    )
    test_sampler = DistributedSampler(testset, num_replicas=world_size, rank=rank)
    train = DataLoader(
        trainset, batch_size=32, shuffle=False, num_workers=1, sampler=train_sampler
    )
    valid = DataLoader(
        testset, batch_size=64, shuffle=False, num_workers=1, sampler=test_sampler
    )
    return train, valid
