# python3.8
"""Contains the class to evaluate 3D-aware GANs by saving snapshots.

Basically, this class traces the quality of multi-view images synthesized by
3D-aware GANs.
"""

import os.path
import numpy as np

import torch
import torch.nn.functional as F

from utils.visualizers import GridVisualizer
from utils.image_utils import postprocess_image
from .base_gan_metric import BaseGANMetric
from models.rendering.point_sampler import sample_camera_extrinsics

__all__ = ['GANSnapshotMultiView']


class GANSnapshotMultiView(BaseGANMetric):
    """Defines the class for saving multi-view images synthesized by GANs."""

    def __init__(self,
                 name='snapshot_multi_view',
                 work_dir=None,
                 logger=None,
                 tb_writer=None,
                 batch_size=1,
                 latent_num=-1,
                 latent_dim=512,
                 latent_codes=None,
                 label_dim=0,
                 labels=None,
                 seed=0,
                 min_val=-1.0,
                 max_val=1.0,
                 radius=1.0,
                 azimuthal_start=np.pi/2-0.6,
                 azimuthal_end=np.pi/2+0.6):
        """Initializes the class with number of samples for each snapshot.

        Args:
            latent_num: Number of latent codes used for each snapshot.
                (default: -1)
            min_val: Minimum pixel value of the synthesized images. This field
                is particularly used for image visualization. (default: -1.0)
            max_val: Maximum pixel value of the synthesized images. This field
                is particularly used for image visualization. (default: 1.0)
        """
        super().__init__(name=name,
                         work_dir=work_dir,
                         logger=logger,
                         tb_writer=tb_writer,
                         batch_size=batch_size,
                         latent_num=latent_num,
                         latent_dim=latent_dim,
                         latent_codes=latent_codes,
                         label_dim=label_dim,
                         labels=labels,
                         seed=seed)
        self.min_val = min_val
        self.max_val = max_val
        self.visualizer = GridVisualizer()
        self.radius = radius
        self.azimuthal_start = azimuthal_start
        self.azimuthal_end = azimuthal_end

    def synthesize(self, generator, generator_kwargs):
        """Synthesizes image with the generator."""
        latent_num = self.latent_num
        batch_size = 1
        if self.random_latents:
            g1 = torch.Generator(device=self.device)
            g1.manual_seed(self.seed)
        else:
            latent_codes = np.load(self.latent_file)[self.replica_indices]
            latent_codes = torch.from_numpy(latent_codes).to(torch.float32)
        if self.random_labels:
            g2 = torch.Generator(device=self.device)
            g2.manual_seed(self.seed)
        else:
            labels = np.load(self.label_file)[self.replica_indices]
            labels = torch.from_numpy(labels).to(torch.float32)

        G = generator
        G_kwargs = generator_kwargs
        G_mode = G.training  # save model training mode.
        G.eval()

        self.logger.info(f'Synthesizing {latent_num} images {self.log_tail}.',
                         is_verbose=True)
        self.logger.init_pbar()
        pbar_task = self.logger.add_pbar_task('Synthesis', total=latent_num)
        all_images = []
        for start in range(0, self.replica_latent_num, batch_size):
            end = min(start + batch_size, self.replica_latent_num)
            with torch.no_grad():
                batch_codes = torch.randn((end - start, *self.latent_dim),
                                            generator=g1, device=self.device)
                if self.random_labels:
                    if self.label_dim == 0:
                        batch_labels = torch.zeros((end - start, 0),
                                                   device=self.device)
                    else:
                        rnd_labels = torch.randint(
                            low=0, high=self.label_dim, size=(end - start,),
                            generator=g2, device=self.device)
                        batch_labels = F.one_hot(
                            rnd_labels, num_classes=self.label_dim)
                else:
                    batch_labels = labels[start:end].cuda().detach()
                for azimuthal in np.linspace(self.azimuthal_start,
                                             self.azimuthal_end, 8):
                    cam2world_matrix = sample_camera_extrinsics(
                        batch_size=(end - start),
                        radius_strategy='fix',
                        radius_fix=self.radius,
                        polar_strategy='fix',
                        polar_fix=np.pi / 2,
                        azimuthal_strategy='fix',
                        azimuthal_fix=azimuthal)['cam2world_matrix']
                    G_kwargs.update(cam2world_matrix=cam2world_matrix)
                    batch_images = G(batch_codes, batch_labels,
                                     **G_kwargs)['image']
                    gathered_images = self.gather_batch_results(batch_images)
                    self.append_batch_results(gathered_images, all_images)
            self.logger.update_pbar(pbar_task, (end - start) * self.world_size)
        self.logger.close_pbar()
        all_images = self.gather_all_results(all_images)[:(latent_num * 8)]

        if self.is_chief:
            assert all_images.shape[0] == (latent_num * 8)
        else:
            assert len(all_images) == 0
            all_images = None

        if G_mode:
            G.train()  # restore model training mode.

        self.sync()
        return all_images

    def evaluate(self, _data_loader, generator, generator_kwargs):
        images = self.synthesize(generator, generator_kwargs)
        if self.is_chief:
            result = {self.name: images}
        else:
            assert images is None
            result = None
        self.sync()
        return result

    def _is_better_than(self, metric_name, new, ref):
        """GAN snapshot is not supposed to judge performance."""
        return None

    def save(self, result, target_filename=None, log_suffix=None, tag=None):
        if not self.is_chief:
            assert result is None
            self.sync()
            return

        assert isinstance(result, dict)
        images = result[self.name]
        assert isinstance(images, np.ndarray)
        images = postprocess_image(
            images, min_val=self.min_val, max_val=self.max_val)
        filename = target_filename or self.name
        save_path = os.path.join(self.work_dir, f'{filename}.png')
        self.visualizer.visualize_collection(images, save_path, num_cols=8)

        prefix = f'Evaluating `{self.name}` with {self.latent_num} samples'
        if log_suffix is None:
            msg = f'{prefix}.'
        else:
            msg = f'{prefix}, {log_suffix}.'
        self.logger.info(msg)

        # Save to TensorBoard if needed.
        if self.tb_writer is not None:
            if tag is None:
                self.logger.warning('`Tag` is missing when writing data to '
                                    'TensorBoard, hence, the data may be mixed '
                                    'up!')
            self.tb_writer.add_image(self.name, self.visualizer.grid, tag,
                                     dataformats='HWC')
            self.tb_writer.flush()
        self.sync()