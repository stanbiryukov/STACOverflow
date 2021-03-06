# -*- coding: utf-8 -*-
"""floodwater_dd.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1W1Kw8eCdJ1oFY9KCCYtnwB4UBovTd5vN

# Flood Prediction - Data Driven
# Stan Biryukov & Dr. Fisch Cheng
"""

! pip install pytorch-lightning==1.3.8 pytorch-lightning-bolts==0.3.2 gpytorch==1.5.0 rasterio==1.2.6 torchgeometry==0.1.2 imagecodecs==2021.7.30 --quiet

! pip install torchvision==0.10.0+cu102 timm==0.4.12 Pillow==7.1.2 kornia==0.5.3 pystiche==1.0.0.post0 segmentation_models_pytorch==0.2.0

! pip install git+https://github.com/PyTorchLightning/lightning-bolts@256ca700870e5df9517501f92e7a3150024c4a07 --quiet

! pip install git+https://github.com/PyTorchLightning/lightning-flash@5f11ebc3ff6a60ea65cfedc07f4a774cb6906a24 --quiet
! pip install pytorch-lightning==1.3.8 --quiet

"""# Mount drive"""

from google.colab import drive
drive.mount('/content/drive')

import os
os.makedirs('/content/drive/MyDrive/flood_dd/', mode=0o777, exist_ok=True)

"""## Get download links from here driven data competition. AWS token updated ~1 day."""

! wget -O /content/flood-train-images.tgz --no-check-certificate --no-proxy "https://drivendata-prod.s3.amazonaws.com/data/81/public/flood-train-images.tgz?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIARVBOBDCYVI2LMPSY%2F20210824%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20210824T154607Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host&X-Amz-Signature=353b667afd802b2ab8a2829ab3b93be4e798122d74b1f5e21d77c79872ddddc3" \
    && tar -xzf /content/flood-train-images.tgz \
    && rm /content/flood-train-images.tgz

! wget -O /content/flood-train-labels.tgz --no-check-certificate --no-proxy "https://drivendata-prod.s3.amazonaws.com/data/81/public/flood-train-labels.tgz?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIARVBOBDCYVI2LMPSY%2F20210824%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20210824T154607Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host&X-Amz-Signature=a5cd589308d54f9162f2316bd4f49d68084010a336d198ba127a188180c771bb" \
    && tar -xzf /content/flood-train-labels.tgz \
    && rm /content/flood-train-labels.tgz

! wget -O /content/drive/MyDrive/flood_dd/flood-train-metadata.csv --no-check-certificate --no-proxy "https://drivendata-prod.s3.amazonaws.com/data/81/public/flood-training-metadata.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIARVBOBDCYVI2LMPSY%2F20210817%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20210817T184602Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host&X-Amz-Signature=a57abbbdfbd89dc43b0307c0056666c5ef41eef2f13177d34bb1d73fbfbbf212"

! nvidia-smi -L

# Commented out IPython magic to ensure Python compatibility.
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import warnings
import random
import copy
import os
import glob

warnings.filterwarnings("ignore")
# %matplotlib inline
# %config InlineBackend.figure_format = 'retina'
# %env HV_DOC_HTML=true

import albumentations
import torch
import rasterio

def season(date, hemisphere='north'):
    md = date.month * 100 + date.day
    if ((md > 320) and (md < 621)):
        s = 0 #spring
    elif ((md > 620) and (md < 923)):
        s = 1 #summer
    elif ((md > 922) and (md < 1223)): 
        s = 2 #fall
    else:
        s = 3 #winter
    if not hemisphere == 'north':
        s = (s + 2) % 4
    return s

"""# Follow data prep from here: https://www.drivendata.co/blog/detect-floodwater-benchmark/

## each set of two polarizations (vh and vh) correspond with a single water label.
"""

train_metadata = pd.read_csv('/content/drive/MyDrive/flood_dd/flood-train-metadata.csv', parse_dates=["scene_start"])
train_metadata['year'] = train_metadata['scene_start'].dt.year
train_metadata.head()

train_metadata.location.unique()

DATA_PATH = "/content/"
! ls {DATA_PATH}

train_metadata["feature_path"] = f"{DATA_PATH}/train_features/" + train_metadata['image_id'] + ".tif"
train_metadata["label_path"] = f"{DATA_PATH}/train_labels/" + train_metadata['chip_id'] + ".tif"
train_metadata.head()

# Helper functions for visualizing Sentinel-1 images
def scale_img(matrix):
    """
    Returns a scaled (H, W, D) image that is visually inspectable.
    Image is linearly scaled between min_ and max_value, by channel.

    Args:
        matrix (np.array): (H, W, D) image to be scaled

    Returns:
        np.array: Image (H, W, 3) ready for visualization
    """
    # Set min/max values
    min_values = np.array([-23, -28, 0.2])
    max_values = np.array([0, -5, 1])

    # Reshape matrix
    w, h, d = matrix.shape
    matrix = np.reshape(matrix, [w * h, d]).astype(np.float64)

    # Scale by min/max
    matrix = (matrix - min_values[None, :]) / (
        max_values[None, :] - min_values[None, :]
    )
    matrix = np.reshape(matrix, [w, h, d])

    # Limit values to 0/1 interval
    return matrix.clip(0, 1)


def create_false_color_composite(path_vv, path_vh):
    """
    Returns a S1 false color composite for visualization.

    Args:
        path_vv (str): path to the VV band
        path_vh (str): path to the VH band

    Returns:
        np.array: image (H, W, 3) ready for visualization
    """
    # Read VV/VH bands
    with rasterio.open(path_vv) as vv:
        vv_img = vv.read(1)
    with rasterio.open(path_vh) as vh:
        vh_img = vh.read(1)

    # Stack arrays along the last dimension
    s1_img = np.stack((vv_img, vh_img), axis=-1)

    # Create false color composite
    img = np.zeros((512, 512, 3), dtype=np.float32)
    img[:, :, :2] = s1_img.copy()
    img[:, :, 2] = s1_img[:, :, 0] / s1_img[:, :, 1]

    return scale_img(img)


def display_random_chip(random_state):
    """
    Plots a 3-channel representation of VV/VH polarizations as a single chip (image 1).
    Overlays a chip's corresponding water label (image 2).

    Args:
        random_state (int): random seed used to select a chip

    Returns:
        plot.show(): chip and labels plotted with pyplot
    """
    f, ax = plt.subplots(1, 2, figsize=(11, 11))

    # Select a random chip from train_metadata
    random_chip = train_metadata.chip_id.sample(random_state=random_state).values[0]
    chip_df = train_metadata[train_metadata.chip_id == random_chip]

    # Extract paths to image files
    vv_path = chip_df[chip_df.polarization == "vv"].feature_path.values[0]
    vh_path = chip_df[chip_df.polarization == "vh"].feature_path.values[0]
    label_path = chip_df.label_path.values[0]

    # Create false color composite
    s1_img = create_false_color_composite(vv_path, vh_path)

    # Visualize features
    ax[0].imshow(s1_img)
    ax[0].set_title("S1 Chip", fontsize=14)

    # Load water mask
    with rasterio.open(label_path) as lp:
        lp_img = lp.read(1)

    # Mask missing data and 0s for visualization
    label = np.ma.masked_where((lp_img == 0) | (lp_img == 255), lp_img)

    # Visualize water label
    ax[1].imshow(s1_img)
    ax[1].imshow(label, cmap="cool", alpha=1)
    ax[1].set_title("S1 Chip with Water Label", fontsize=14)

    plt.tight_layout(pad=5)
    plt.show()

display_random_chip(7)

"""##  let's confirm that the first few training images are the expected size of 512 x 512 pixels."""

examples = [rasterio.open(train_metadata.feature_path[x]) for x in range(5)]
for image in examples:
    print(image.shape)

train_metadata.head()

train_metadata.scene_start.unique()

"""## pivot wide"""

dw = train_metadata.pivot(index=['chip_id', 'location', 'year', 'scene_start', 'label_path'], columns='polarization', values=['feature_path']).reset_index()
dw.head()

from sklearn.model_selection import train_test_split

# split train test val
train_ratio = 0.60
validation_ratio = 0.20
test_ratio = 0.20

train, test = train_test_split(dw, test_size = 1- train_ratio, random_state=5235, stratify=dw[['location', 'year']])
val, test = train_test_split(test, test_size=test_ratio/(test_ratio + validation_ratio), random_state=7567, stratify=test[['location', 'year']])

train.shape

test.shape

val

train

"""## Torch DataLoader"""

import pytorch_lightning as pl
from torch.utils.data import DataLoader
from torchvision import transforms
from tqdm.auto import tqdm
import multiprocessing

class FloodDataset(torch.utils.data.Dataset):
    """Reads in images, transforms pixel values, and serves a
    dictionary containing chip ids, image tensors, and
    label masks (where available).
    """

    def __init__(self, x_paths, y_paths=None, transforms=None):
        self.data = x_paths
        self.label = y_paths
        self.transforms = transforms

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        # Loads a 2-channel image from a chip-level dataframe
        img = self.data.iloc[idx]

        with rasterio.open(img['feature_path']['vv']) as vv:
            vv_path = vv.read(1)
        with rasterio.open(img['feature_path']['vh']) as vh:
            vh_path = vh.read(1)
        x_arr = np.stack([vv_path, vh_path], axis=-1) # stack two poplarization together

        # Min-max normalization
        min_norm = -77
        max_norm = 26
        x_arr = np.clip(x_arr, min_norm, max_norm)
        x_arr = (x_arr - min_norm) / (max_norm - min_norm)

        # Apply data augmentations, if provided
        if self.transforms:
            x_arr = self.transforms(image=x_arr)["image"]
        
        x_arr = np.transpose(x_arr, [2, 0, 1]) # [N, C, H, W]

        h, w = x_arr.shape[-2], x_arr.shape[-1]
        # Prepare sample dictionary.
        sample = {"chip_id": img.chip_id.item(), "input": torch.as_tensor(x_arr).type(torch.FloatTensor), "metadata": {"size": (h, w)}}

        # Load label if available - training only
        if self.label is not None:
            label_path = self.label.iloc[idx]
            with rasterio.open(label_path['label_path'].item()) as lp:
                y_arr = lp.read(1)
            # Apply same data augmentations to label
            if self.transforms:
                y_arr = self.transforms(image=y_arr)["image"]

            # mask 255 as missing
            y_arr = torch.from_numpy(y_arr).type(torch.LongTensor) # [N, H, W]
            mask_ = y_arr.ne(255)
            sample["target"] = torch.mul(y_arr, mask_)

        return sample # so each sample is a dictionary with four keys: chip_id, input, metadata and target

# Pretty straightfoward this
class FloodDataModule(pl.LightningDataModule):
    def __init__(self, train, test, val, transforms=None, num_workers=max(multiprocessing.cpu_count() - 1, 1), shuffle=False, batch_size: int = 16, pin_memory=True, drop_last=False):
        super().__init__()
        self.num_workers = num_workers
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.pin_memory = pin_memory
        self.drop_last = drop_last

        self.train = train
        self.test = test
        self.val = val
        self.transforms = transforms
        self.flood_train = FloodDataset(x_paths = self.train[['chip_id', 'feature_path']], y_paths = self.train[['chip_id', 'label_path']], transforms=self.transforms)
        self.flood_test = FloodDataset(x_paths = self.test[['chip_id', 'feature_path']], y_paths = self.test[['chip_id', 'label_path']], transforms=self.transforms)
        self.flood_val = FloodDataset(x_paths = self.val[['chip_id', 'feature_path']], y_paths = self.val[['chip_id', 'label_path']], transforms=self.transforms)

    def train_dataloader(self):
        return DataLoader(
            self.flood_train,
            batch_size=self.batch_size,
            shuffle=self.shuffle,
            num_workers=self.num_workers,
            drop_last=self.drop_last,
            pin_memory=self.pin_memory
            )
        
    def val_dataloader(self):
        return DataLoader(
            self.flood_val,
            batch_size=self.batch_size,
            shuffle=self.shuffle,
            num_workers=self.num_workers,
            drop_last=self.drop_last,
            pin_memory=self.pin_memory
            )

    def test_dataloader(self):
        return DataLoader(
            self.flood_test,
            batch_size=self.batch_size,
            shuffle=self.shuffle,
            num_workers=self.num_workers,
            drop_last=self.drop_last,
            pin_memory=self.pin_memory
            )

# Example use of albumations for transforming images
training_transformations = albumentations.Compose(
    [
        albumentations.RandomCrop(256, 256),
        albumentations.RandomRotate90(),
        albumentations.HorizontalFlip(),
        albumentations.VerticalFlip(),
    ]
)

"""## Test Data Loader"""

from itertools import islice
tx_ = FloodDataset(x_paths = train[['chip_id', 'feature_path']], y_paths = train[['chip_id', 'label_path']], transforms=None)
# tx_ = FloodDataset(x_paths = train[['chip_id', 'feature_path']], y_paths = train[['chip_id', 'label_path']], transforms=training_transformations)

# print first 1
for i in islice(tx_, 2, 3):
    print(i)

i['target'].max()

i['target'].unique()

i['target'].shape

mask_ = i['target'].ne(255)
mask_.shape

i['target'].shape[0]

torch.mul(i['target'], mask_)

# fdatamodule = FloodDataModule(train = train, test = test, val = val, transforms=training_transformations)
fdatamodule = FloodDataModule(train = train, test = test, val = val, batch_size=1, transforms=None)
i = next(iter(fdatamodule.train_dataloader()))

i

i['input'].shape

i['input'].size()

i['target'].shape

i['target'][0, :, :].max()

i['target'][:, :, :].max()

i['input'][:, :, :, :].max()

i['input'][:, :, :, :].min()

import time
import flash
from flash.image import SemanticSegmentation, SemanticSegmentationData
from functools import partial

from pytorch_lightning import seed_everything
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.loggers import TensorBoardLogger
from pytorch_lightning.tuner.tuning import Tuner

from flash.core.utilities.imports import _SEGMENTATION_MODELS_AVAILABLE
from flash.image.segmentation import SemanticSegmentation
from flash.image.segmentation.backbones import SEMANTIC_SEGMENTATION_BACKBONES
from flash.image.segmentation.heads import SEMANTIC_SEGMENTATION_HEADS

# available_weights = SemanticSegmentation.available_pretrained_weights("resnet18")
# backbone = SEMANTIC_SEGMENTATION_BACKBONES.get("resnet18")()
# SEMANTIC_SEGMENTATION_HEADS.get("unet")(backbone=backbone, in_channels=2, num_classes=10, pretrained=True)

# rng = np.random.default_rng(seed=4724)
# rng.choice(available_weights, 1)[0]

SEMANTIC_SEGMENTATION_BACKBONES.available_keys()

import gc
gc.collect()
torch.cuda.empty_cache()

"""## core models here: https://smp.readthedocs.io/en/latest/encoders.html"""

from torchgeometry.losses import dice_loss

from pl_bolts.losses.object_detection import giou_loss

# ! kill -9 7654
# ! kill $(ps -e | grep 'tensorboard' | awk '{print $1}')

! ls /content/drive/MyDrive/flood_dd/

# Commented out IPython magic to ensure Python compatibility.
# launch tensorboard.
# %load_ext tensorboard
# %reload_ext tensorboard
# %tensorboard --logdir lightning_logs/

"""## Fine tune/transfer learning of segmentation model"""

seed_everything(8708, workers=True)

backbone        = "tf_efficientnet_lite4"
head            = "unet"
encoder_weights = "imagenet"

model = SemanticSegmentation(
    backbone=backbone,
    head=head,
    pretrained=encoder_weights,
    head_kwargs={"in_channels": 2,},
    num_classes=2,
    optimizer=torch.optim.AdamW,
)

fdatamodule = FloodDataModule(train = train, test = test, val = val, batch_size=3, transforms=None)
trainer = flash.Trainer(gpus=-1, max_epochs=1, stochastic_weight_avg=True, deterministic=True, callbacks=[EarlyStopping(monitor="val_iou", mode="max", patience=10)])
trainer.finetune(model, datamodule=fdatamodule, strategy="freeze")

"""## can't get `auto_scale_batch_size` working w/ datamodule so roll my own."""

def find_batch_size(model, datamodule):

    batch_sizes_ = [int(2 ** x) for x in np.arange(0, 10)][::-1]

    for b in batch_sizes_:
        fdatamodule = copy.deepcopy(datamodule)
        try:
            fdatamodule.num_workers = 0
            fdatamodule.batch_size = b

            trainer = flash.Trainer(gpus=-1, fast_dev_run=True, log_gpu_memory=True)
            trainer.finetune(model, datamodule=fdatamodule, strategy="freeze")

            break
        except Exception as e:
            print(f"Failed w/ {b} ")
            gc.collect()
            torch.cuda.empty_cache()

    gc.collect()
    torch.cuda.empty_cache()
    return b

new_batch_size = find_batch_size(model = model, datamodule = fdatamodule)

new_batch_size

start = time.time()
fdatamodule = FloodDataModule(train = train, test = test, val = val, batch_size = int(max(new_batch_size//2, 1)), transforms=None)
logger = TensorBoardLogger(save_dir='lightning_logs/', name=f'{backbone}-{head}-{encoder_weights}')
trainer = flash.Trainer(gpus=-1, logger=logger, log_gpu_memory=True, stochastic_weight_avg=True, deterministic=True, callbacks=[EarlyStopping(monitor="val_iou", mode="max", patience=10)])
trainer.finetune(model, datamodule=fdatamodule, strategy="freeze")
lap = time.time()

# ! tensorboard dev list

# resnext101_32x8d
# unet: val_iou=0.794
# fpn: 0.670
# tf_efficientnet_lite4
# unet: 0.775

trainer.save_checkpoint(f"/content/drive/MyDrive/flood_dd/{backbone}-{head}-{encoder_weights}.pt")

print(f"Training took {lap - start:.2f} seconds")

"""## predict. note it returns python list of 0/1 for each image and expects `DataLoader`"""

seed_everything(8708, workers=True)
testx = test.sample(n=5, random_state=345)
hats = model.predict(x=FloodDataset(x_paths = testx[['chip_id', 'feature_path']], y_paths = testx[['chip_id', 'label_path']], transforms=None))
hats = [torch.from_numpy(np.array(x).astype(np.float32)) for x in hats]

[ (x.min(), x.max()) for x in hats]

def display_prediction(df, hat):
    """
    Plots a 3-channel representation of VV/VH polarizations as a single chip (image 1).
    Overlays a chip's corresponding water label (image 2).

    Args:
        random_state (int): random seed used to select a chip

    Returns:
        plot.show(): chip and labels plotted with pyplot
    """
    f, ax = plt.subplots(1, 3, figsize=(16, 16))

    testx = df.copy()

    # Extract paths to image files
    vv_path = testx['feature_path']['vv']
    vh_path = testx['feature_path']['vh']
    label_path = testx['label_path'].item()

    # Create false color composite
    s1_img = create_false_color_composite(vv_path, vh_path)

    # Visualize features
    ax[0].imshow(s1_img)
    ax[0].set_title("S1 Chip", fontsize=14)

    # Load water mask
    with rasterio.open(label_path) as lp:
        lp_img = lp.read(1)

    # Mask missing data and 0s for visualization
    label = np.ma.masked_where((lp_img == 0) | (lp_img == 255), lp_img)

    # Visualize water label
    ax[1].imshow(s1_img)
    ax[1].imshow(label, cmap="cool", alpha=1)
    ax[1].set_title("S1 Chip with Water Label", fontsize=14)

    # resize array
    # note torch.Upsample requires additional dimension
    hat_img = np.rint(torch.nn.functional.interpolate(hat.reshape( (1,) * 1 + hat.shape ).type(torch.FloatTensor).unsqueeze(1), size=(s1_img.shape[0], s1_img.shape[1]), mode='bicubic').squeeze().numpy()).astype('uint8')

    # Prediction
    hat_label = np.ma.masked_where((hat_img == 0), hat_img)
    ax[2].imshow(s1_img)
    ax[2].imshow(hat_label, cmap="spring", alpha=1)
    ax[2].set_title("S1 Chip with Prediction", fontsize=14)
    plt.tight_layout()
    plt.show()

display_prediction(testx.iloc[2], hats[2])

display_prediction(testx.iloc[0], hats[0])

display_prediction(testx.iloc[1], hats[1])

display_prediction(testx.iloc[3], hats[3])

display_prediction(testx.iloc[4], hats[4])

import dill

def pl_callback_to_dict(trainer):
    '''
    Save off callback summary into dict
    '''
    metrics_ = trainer.callback_metrics.copy()
    for k, v in metrics_.items():
        metrics_[k] = float(v.detach().cpu().numpy())
    return metrics_

os.makedirs('/content/drive/MyDrive/flood_dd/metrics/', mode=0o777, exist_ok=True)

metrics_ = pl_callback_to_dict(trainer)
metrics_['model'] = f"{backbone}-{head}-{encoder_weights}"

with open(f"/content/drive/MyDrive/flood_dd/metrics/{backbone}-{head}-{encoder_weights}.dill", "wb") as f: 
    dill.dump(metrics_, f)

with open(f"/content/drive/MyDrive/flood_dd/metrics/{backbone}-{head}-{encoder_weights}.dill", "rb") as f: 
    metrics_ = dill.load(f)

metrics_

"""# Loop through and run models
## Here, the goal is to select the best performing architecture. Then we can try unet/fpn, and optimize further.
"""

from tqdm.auto import tqdm

def _run_model(backbone, weights, datamodule, head="unet"):

    seed_everything(8708, workers=True)

    backbone        = backbone
    head            = head
    encoder_weights = weights

    logger = TensorBoardLogger(save_dir="lightning_logs/", name=f'{backbone}-{head}-{encoder_weights}')

    model = SemanticSegmentation(
        backbone=backbone,
        head=head,
        pretrained=encoder_weights,
        head_kwargs={"in_channels": 2,},
        num_classes=2,
        optimizer=torch.optim.AdamW,
    )

    # new_batch_size = find_batch_size(model = model, datamodule = datamodule)
    # datamodule.batch_size = int(max(new_batch_size//2, 1)) # div `new_batch_size` by 2 for epoch overhead

    gc.collect()
    torch.cuda.empty_cache()

    print(f"Starting {backbone}-{head}-{encoder_weights}")

    # print(f"Starting {backbone}-{head}-{encoder_weights} with {new_batch_size} batch size")

    trainer = flash.Trainer(gpus=-1, logger=logger, log_gpu_memory=True, stochastic_weight_avg=True, deterministic=True, callbacks=[EarlyStopping(monitor="val_iou", mode="max", patience=10)])
    start = time.time()
    trainer.finetune(model, datamodule=datamodule, strategy="freeze")
    lap = time.time()
    print(f"Fine tuning {backbone}-{head}-{encoder_weights} took {lap - start:.2f} seconds")

    # save
    metrics_ = pl_callback_to_dict(trainer)
    metrics_['model'] = f"{backbone}-{head}-{encoder_weights}"

    with open(f"/content/drive/MyDrive/flood_dd/metrics/{backbone}-{head}-{encoder_weights}.dill", "wb") as f: 
        dill.dump(metrics_, f)

    trainer.save_checkpoint(f"/content/drive/MyDrive/flood_dd/{backbone}-{head}-{encoder_weights}.pt")


def run_models(mdl_list, datamodule):
    for m in tqdm(mdl_list):
        # get the weight options
        weight_list = SemanticSegmentation.available_pretrained_weights(m)
        for w in tqdm(weight_list, leave=False):
            if not os.path.exists(f"/content/drive/MyDrive/flood_dd/{m}-unet-{w}.pt"):
                print(f"Running model {m}-unet-{w}")
                gc.collect()
                torch.cuda.empty_cache()
                try:
                    _run_model(backbone = m, weights = w, datamodule = datamodule, head="unet")
                except Exception as e:
                    print(e)
                    gc.collect()
                    torch.cuda.empty_cache()

# Commented out IPython magic to ensure Python compatibility.
# %%time
# mdl_list = SEMANTIC_SEGMENTATION_BACKBONES.available_keys()
# fdatamodule = FloodDataModule(train = train, test = test, val = val, batch_size=1, transforms=None)
# run_models(mdl_list, datamodule=fdatamodule)

mdl_list

import glob
out = glob.glob('/content/drive/MyDrive/flood_dd/metrics/*.dill')

# [x for x in out if x not in mdl_list]

# out = [os.path.basename(x).replace('.pt', '') for x in out]

[x for x in mdl_list if not any(z for z in out)]

sorted(out)

[x for x in out if not any(w in x for w in mdl_list)]

len(out)

len(mdl_list)

with open(out[0], "rb") as f: 
    metrics_ = dill.load(f)

{out[0]: metrics_}

def _parse_dill(x):
    with open(x, "rb") as f: 
        metrics_ = dill.load(f)
    return {x: metrics_}

metrics = [_parse_dill(x) for x in out]
metrics = {k:v for x in metrics for k, v in x.items()}

dfmetrics = pd.DataFrame(metrics).T.sort_values('val_iou', ascending=False)

dfmetrics.head(10)

dfmetrics.iloc[0]

# Commented out IPython magic to ensure Python compatibility.
# launch tensorboard for tuning loc.
# %load_ext tensorboard
# %reload_ext tensorboard
# %tensorboard --logdir lightning_tune_logs/ --port 6007

# from tensorboard import notebook
# notebook.list() # View open TensorBoard instances

# notebook.display(port=6006, height=1000)

"""## Best model - `tf_efficientnet_lite4` with `unet` head."""

seed_everything(8708, workers=True)

backbone        = "tf_efficientnet_lite4"
head            = "unet"
encoder_weights = "imagenet"

logger = TensorBoardLogger(save_dir="lightning_tune_logs/", name=f'{backbone}-{head}-{encoder_weights}')

model = SemanticSegmentation(
    backbone=backbone,
    head=head,
    pretrained=encoder_weights,
    head_kwargs={"in_channels": 2,},
    num_classes=2,
    optimizer=torch.optim.AdamW,
)

fdatamodule = FloodDataModule(train = train, test = test, val = val, batch_size=3, transforms=None)
trainer = flash.Trainer(gpus=-1, logger=logger, stochastic_weight_avg=True, deterministic=True, callbacks=[EarlyStopping(monitor="val_iou", mode="max", patience=10)])
trainer.finetune(model, datamodule=fdatamodule, strategy="freeze")

"""## Run all heads"""

import segmentation_models_pytorch as smp

SMP_MODEL_CLASS = [
    smp.Unet,
    smp.UnetPlusPlus,
    smp.MAnet,
    smp.Linknet,
    smp.FPN,
    smp.PSPNet,
    smp.DeepLabV3,
    smp.DeepLabV3Plus,
    smp.PAN,
]

SMP_MODELS = {a.__name__.lower(): a for a in SMP_MODEL_CLASS}
SMP_MODELS

def _run_model_tune(datamodule, backbone="tf_efficientnet_lite4", weights="imagenet", head="unet"):

    seed_everything(8708, workers=True)

    backbone        = backbone
    head            = head
    encoder_weights = weights

    logger = TensorBoardLogger(save_dir="lightning_tune_logs/", name=f'{backbone}-{head}-{encoder_weights}')

    model = SemanticSegmentation(
        backbone=backbone,
        head=head,
        pretrained=encoder_weights,
        head_kwargs={"in_channels": 2,},
        num_classes=2,
        optimizer=torch.optim.AdamW,
    )

    gc.collect()
    torch.cuda.empty_cache()

    print(f"Starting {backbone}-{head}-{encoder_weights}")

    trainer = flash.Trainer(gpus=-1, logger=logger, log_gpu_memory=True, stochastic_weight_avg=True, deterministic=True, callbacks=[EarlyStopping(monitor="val_iou", mode="max", patience=10)])
    start = time.time()
    trainer.finetune(model, datamodule=datamodule, strategy="freeze")
    lap = time.time()
    print(f"Fine tuning {backbone}-{head}-{encoder_weights} took {lap - start:.2f} seconds")

    # save
    metrics_ = pl_callback_to_dict(trainer)
    metrics_['model'] = f"{backbone}-{head}-{encoder_weights}"

    with open(f"/content/drive/MyDrive/flood_dd/metrics/tune/{backbone}-{head}-{encoder_weights}.dill", "wb") as f: 
        dill.dump(metrics_, f)

    trainer.save_checkpoint(f"/content/drive/MyDrive/flood_dd/tune/{backbone}-{head}-{encoder_weights}.pt")


def run_tune(heads, datamodule):
    for h in tqdm(heads):
        if not os.path.exists(f"/content/drive/MyDrive/flood_dd/tune/tf_efficientnet_lite4-{h}-imagenet.pt"):
            print(f"Running model tf_efficientnet_lite4-{h}-imagenet")
            gc.collect()
            torch.cuda.empty_cache()
            try:
                _run_model_tune(backbone="tf_efficientnet_lite4", weights="imagenet", head=h, datamodule=datamodule)
            except Exception as e:
                print(e)
                gc.collect()
                torch.cuda.empty_cache()

fdatamodule = FloodDataModule(train = train, test = test, val = val, batch_size=3, transforms=None)

# _run_model_tune(backbone="tf_efficientnet_lite4", weights="imagenet", head="unetplusplus", datamodule=fdatamodule)

# Commented out IPython magic to ensure Python compatibility.
# %%time
# run_tune(heads = SMP_MODELS, datamodule = fdatamodule)

tuneout = glob.glob('/content/drive/MyDrive/flood_dd/metrics/tune/*.dill')
tuneout

tunemetrics = [_parse_dill(x) for x in tuneout]
tunemetrics = {k:v for x in tunemetrics for k, v in x.items()}
tunemetrics = pd.DataFrame(tunemetrics).T.sort_values('val_iou', ascending=False)
tunemetrics.head()

tunemetrics.iloc[0]

best_model = tunemetrics.iloc[0]['model']
best_model

model = SemanticSegmentation.load_from_checkpoint(f"/content/drive/MyDrive/flood_dd/tune/{best_model}.pt")

"""## Run predictions on all test data with best model."""

# Commented out IPython magic to ensure Python compatibility.
# %%time
# seed_everything(8708, workers=True)
# hats = model.predict(x=FloodDataset(x_paths = test[['chip_id', 'feature_path']], y_paths = test[['chip_id', 'label_path']], transforms=None))
# hats = [torch.from_numpy(np.array(x).astype(np.float32)) for x in hats]

[display_prediction(test.iloc[x], hats[x]) for x in range(len(hats))]

