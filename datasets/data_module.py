import numpy as np
import omegaconf
import pandas as pd
import torch
from torch_geometric.data import Data, InMemoryDataset
from utils.data.abstract_datatype import (
    AbstractDataModule,
    AbstractDatasetInfos,
    Statistics,
)
from utils.data.load import (
    character_to_int,
    detect_nan_rows,
    position_normalize,
    standardise_dataframe_colnames,
)
from torch.utils.data import DataLoader
from torch_geometric.data import Batch


class Dataset(InMemoryDataset):
    def __init__(
        self,
        split: int,
        input_data: pd.DataFrame,
        root: str = None,
        transform: callable = None,
        pre_transform: callable = None,
        pre_filter: callable = None,
        cfg: omegaconf = None,
    ) -> None:
        super().__init__(root, transform, pre_transform, pre_filter)
        self.split = split
        self.name = cfg.dataset.dataset_name
        self.input_data = input_data
        self.num_cell_class = len(input_data["c"].unique())
        self.cfg = cfg
        self._data, self.slices = Data(), {}

        # Dataset processing pipeline
        self.process_data_split()
        self.process_data()
        self.process_slices()

    def process_data_split(self) -> None:
        train_regions = self.cfg.dataset.train_regions
        validation_regions = self.cfg.dataset.validation_regions
        test_regions = self.cfg.dataset.test_regions
        if self.split == "train":
            self.input_data = self._process_split(train_regions, "train")
        elif self.split == "validation":
            self.input_data = self._process_split(validation_regions, "validation")
        else:
            self.input_data = self._process_split(test_regions, "test")

    def _process_split(self, regions: list, split_type: str) -> pd.DataFrame:
        print(f"Loading {split_type} data")
        return self.input_data.loc[self.input_data["regions"].isin(regions)]

    def process_data(self) -> None:
        self.input_data = self.input_data.sort_values("regions", ignore_index=False)
        gene_names = self.filter_genes()

        # Normalize coordinates
        self.input_data = position_normalize(self.input_data)
        (
            positions,
            node_features,
            cell_class,
            cell_class_decoder,
        ) = self._convert_data_to_tensors(gene_names)

        # Clean NaN rows
        nan_rows = detect_nan_rows(positions)
        clean_positions, clean_node_features, clean_cell_class = self._clean_data(
            positions, node_features, cell_class, nan_rows
        )

        # Update data attributes
        self._update_data_attributes(
            clean_positions,
            clean_node_features,
            clean_cell_class,
            gene_names,
            cell_class_decoder,
        )

    def _convert_data_to_tensors(self, gene_names: list):
        positions = torch.tensor(self.input_data[["x", "y"]].values).float()
        node_features = torch.tensor(self.input_data[gene_names].values).float()
        cell_class = self.input_data["c"]
        unique_class = sorted(list(cell_class.unique()))
        cell_class, cell_class_decoder = character_to_int(
            list(cell_class.values), unique_class
        )
        return positions, node_features, torch.tensor(cell_class), cell_class_decoder

    def _clean_data(self, positions, node_features, cell_class, nan_rows):
        clean_positions = positions[~nan_rows]
        clean_node_features = node_features[~nan_rows]
        clean_cell_class = cell_class[~nan_rows]
        return clean_positions, clean_node_features, clean_cell_class

    def _update_data_attributes(
        self,
        clean_positions,
        clean_node_features,
        clean_cell_class,
        gene_names,
        cell_class_decoder,
    ):
        cell_ID = torch.tensor(self.input_data.index)

        self._data.positions = clean_positions
        self._data.node_features = clean_node_features
        self._data.cell_class = clean_cell_class
        self._data.cell_ID = cell_ID

        num_cell_to_region_mapping_dict = self._create_region_mapping_dict()
        self.statistics = Statistics(
            num_cell_class=self.num_cell_class,
            num_genes=len(gene_names),
            cell_class_decoder=cell_class_decoder,
            num_cell_to_region_mapping_dict=num_cell_to_region_mapping_dict,
        )

    def _create_region_mapping_dict(self):
        num_cell_to_region_mapping_dict = (
            self.input_data.groupby("regions").size().to_dict()
        )
        return {v: k for k, v in num_cell_to_region_mapping_dict.items()}

    def filter_genes(self) -> list:
        gene_columns_start = self.cfg.dataset.gene_columns_start
        gene_columns_end = self.cfg.dataset.gene_columns_end
        gene_names = list(self.input_data.columns[gene_columns_start:gene_columns_end])
        gene_names.sort()
        return gene_names

    def process_slices(self) -> None:
        slice_indices = self._generate_slice_indices()
        slice_ = torch.tensor(slice_indices, dtype=torch.float32)

        self.slices = {
            k: slice_
            for k in [
                "node_features",
                "positions",
                "cell_class",
                "cell_class_features",
                "gene_name_features",
                "cell_ID",
            ]
        }
        print(slice_)

    def _generate_slice_indices(self):
        slices = self.input_data["regions"].values
        current_slice, slice_start, slice_ = slices[0], 0, []

        for i in range(1, len(slices)):
            if slices[i] != current_slice or i == len(slices) - 1:
                slice_end = i if i != len(slices) - 1 else i + 1
                slice_.extend([slice_start, slice_end])
                current_slice, slice_start = slices[i], i

        return np.unique(slice_)


class DataModule(AbstractDataModule):
    def __init__(self, cfg):
        data = self.data_loading(cfg)
        self.train_dataset = self._initialize_dataset("train", data, cfg)
        self.validation_dataset = self._initialize_dataset("validation", data, cfg)
        self.test_dataset = self._initialize_dataset("test", data, cfg)

        self.statistics = {
            "train": self.train_dataset.statistics,
            "validation": self.validation_dataset.statistics,
            "test": self.test_dataset.statistics,
        }
        super().__init__(
            cfg,
            train_dataset=self.train_dataset,
            val_dataset=self.validation_dataset,
            test_dataset=self.test_dataset,
        )

    def _initialize_dataset(self, split, data, cfg):
        return Dataset(split=split, input_data=data, cfg=cfg)

    def collate(self, batch):
        return self._create_batch(batch)

    def _create_batch(self, batch):
        batch_data = Batch()
        batch_data.node_features = torch.cat(
            [data.node_features for data in batch], dim=0
        )
        batch_data.positions = torch.cat([data.positions for data in batch], dim=0)
        batch_data.cell_class = torch.cat([data.cell_class for data in batch], dim=0)
        batch_data.cell_ID = torch.cat([data.cell_ID for data in batch], dim=0)

        batch_data.batch = torch.tensor(
            [
                i
                for i, data in enumerate(batch)
                for _ in range(data.node_features.size(0))
            ],
            dtype=torch.long,
        )
        return batch_data

    def data_loading(self, cfg: omegaconf.DictConfig) -> pd.DataFrame:
        data_path = cfg.dataset.data_path
        input_data = pd.read_csv(f"{data_path}", index_col=0)
        return standardise_dataframe_colnames(cfg.dataset, input_data)


class Infos(AbstractDatasetInfos):
    """
    Class for storing information about the MERFISH dataset.

    This class encapsulates various statistics and configurations specific to the
    MERFISH dataset, aiding in dataset handling and model training processes.

    Attributes:
        datamodule: Instance of the data module associated with MERFISH data.
        cfg: Configuration object containing dataset and model parameters.
    """

    def __init__(self, datamodule, cfg):
        self.input_dims = {}
        self.output_dims = {}
        self.name = cfg.dataset.dataset_name
        self.num_cell_class = datamodule.statistics["train"].num_cell_class
        self.num_genes = datamodule.statistics["train"].num_genes
        self.cell_class_decoder = {}
        self.num_cell_to_region_mapping_dict = {}
        self.cell_class_decoder = datamodule.statistics["test"].cell_class_decoder
        self.num_cell_to_region_mapping_dict = datamodule.statistics[
            "test"
        ].num_cell_to_region_mapping_dict
        self.input_dims["node_features_dimensions"] = self.num_genes
        self.input_dims["diffusion_time_dimensions"] = 1
        self.output_dims["node_features_dimensions"] = self.num_genes
        self.output_dims["diffusion_time_dimensions"] = 0
