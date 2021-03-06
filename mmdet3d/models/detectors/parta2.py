import torch
from torch.nn import functional as F

from mmdet3d.ops import Voxelization
from mmdet.models import DETECTORS
from .. import builder
from .two_stage import TwoStage3DDetector
from .voxelnet import Fusion
import numpy as np


@DETECTORS.register_module()
class PartA2(TwoStage3DDetector):
    r"""Part-A2 detector.

    Please refer to the `paper <https://arxiv.org/abs/1907.03670>`_
    """

    def __init__(self,
                 voxel_layer,
                 voxel_encoder,
                 middle_encoder,
                 backbone,
                 neck=None,
                 rpn_head=None,
                 roi_head=None,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None):
        super(PartA2, self).__init__(
            backbone=backbone,
            neck=neck,
            rpn_head=rpn_head,
            roi_head=roi_head,
            train_cfg=train_cfg,
            test_cfg=test_cfg,
            pretrained=pretrained,
        )
        self.voxel_layer = Voxelization(**voxel_layer)
        self.voxel_encoder = builder.build_voxel_encoder(voxel_encoder)
        self.middle_encoder = builder.build_middle_encoder(middle_encoder)

        # 转换RangeImage部分相关代码
        self.H = 48
        self.W = 512
        self.fov_up = 3
        self.fov_down = -15.0
        self.pi = torch.tensor(np.pi)
        fov_up = self.fov_up * self.pi / 180.0
        fov_down = self.fov_down * self.pi / 180.0
        fov = abs(fov_up) + abs(fov_down)
        self.uv = torch.zeros((2, self.H, self.W))
        self.uv[1] = torch.arange(0, self.W)
        self.uv.permute((0, 2, 1))[0] = torch.arange(0, self.H)
        self.uv[0] = ((self.H - self.uv[0]) * fov - abs(fov_down) * self.H) / self.H
        self.uv[1] = (self.uv[1] * 2.0 - self.W) * self.pi / (self.W * 4)  # 最后一个 4 用来控制水平范围

        # self.range_encoder = RangeEncoder(5, 64, use_img=True)
        self.fusion = Fusion(5, 3)

    def extract_feat(self, points, img_metas):
        """Extract features from points."""
        voxel_dict = self.voxelize(points)
        voxel_features = self.voxel_encoder(voxel_dict['voxels'],
                                            voxel_dict['num_points'],
                                            voxel_dict['coors'])
        batch_size = voxel_dict['coors'][-1, 0].item() + 1
        feats_dict = self.middle_encoder(voxel_features, voxel_dict['coors'],
                                         batch_size)
        x = self.backbone(feats_dict['spatial_features'])
        if self.with_neck:
            neck_feats = self.neck(x)
            feats_dict.update({'neck_feats': neck_feats})
        return feats_dict, voxel_dict

    @torch.no_grad()
    def voxelize(self, points):
        """Apply hard voxelization to points."""
        voxels, coors, num_points, voxel_centers = [], [], [], []
        for res in points:
            res_voxels, res_coors, res_num_points = self.voxel_layer(res)
            res_voxel_centers = (
                res_coors[:, [2, 1, 0]] + 0.5) * res_voxels.new_tensor(
                    self.voxel_layer.voxel_size) + res_voxels.new_tensor(
                        self.voxel_layer.point_cloud_range[0:3])
            voxels.append(res_voxels)
            coors.append(res_coors)
            num_points.append(res_num_points)
            voxel_centers.append(res_voxel_centers)

        voxels = torch.cat(voxels, dim=0)
        num_points = torch.cat(num_points, dim=0)
        voxel_centers = torch.cat(voxel_centers, dim=0)
        coors_batch = []
        for i, coor in enumerate(coors):
            coor_pad = F.pad(coor, (1, 0), mode='constant', value=i)
            coors_batch.append(coor_pad)
        coors_batch = torch.cat(coors_batch, dim=0)

        voxel_dict = dict(
            voxels=voxels,
            num_points=num_points,
            coors=coors_batch,
            voxel_centers=voxel_centers)
        return voxel_dict

    def forward_train(self,
                      points,
                      img_metas,
                      gt_bboxes_3d,
                      gt_labels_3d,
                      img=None,
                      gt_bboxes_ignore=None,
                      proposals=None):
        """Training forward function.

        Args:
            points (list[torch.Tensor]): Point cloud of each sample.
            img_metas (list[dict]): Meta information of each sample
            gt_bboxes_3d (list[:obj:`BaseInstance3DBoxes`]): Ground truth
                boxes for each sample.
            gt_labels_3d (list[torch.Tensor]): Ground truth labels for
                boxes of each sampole
            gt_bboxes_ignore (list[torch.Tensor], optional): Ground truth
                boxes to be ignored. Defaults to None.

        Returns:
            dict: Losses of each branch.
        """

        # 转换成range提取特征后转回lidar
        batchsize = len(points)
        rangeImage = []
        for i in range(batchsize):
            rangeImage.append(self.lidar_to_range_gpu(points[i]).unsqueeze(0))
        rangeImage = torch.cat(rangeImage, dim=0)
        # 是否加入img信息
        # range_feat = self.range_encoder(rangeImage, img)      #用自编码器的形式
        range_feat = self.fusion(rangeImage, img)
        range_ori = torch.cat((rangeImage[:, 0:2], range_feat), dim=1)
        pts_with_range = []
        for i in range(batchsize):
            pts_with_range.append(self.range_to_lidar_gpu(range_ori[i].squeeze(0)))

        feats_dict, voxels_dict = self.extract_feat(pts_with_range, img_metas)      #point 换成pts_with_range

        losses = dict()

        if self.with_rpn:
            rpn_outs = self.rpn_head(feats_dict['neck_feats'])
            rpn_loss_inputs = rpn_outs + (gt_bboxes_3d, gt_labels_3d,
                                          img_metas)
            rpn_losses = self.rpn_head.loss(
                *rpn_loss_inputs, gt_bboxes_ignore=gt_bboxes_ignore)
            losses.update(rpn_losses)

            proposal_cfg = self.train_cfg.get('rpn_proposal',
                                              self.test_cfg.rpn)
            proposal_inputs = rpn_outs + (img_metas, proposal_cfg)
            proposal_list = self.rpn_head.get_bboxes(*proposal_inputs)
        else:
            proposal_list = proposals

        roi_losses = self.roi_head.forward_train(feats_dict, voxels_dict,
                                                 img_metas, proposal_list,
                                                 gt_bboxes_3d, gt_labels_3d)

        losses.update(roi_losses)

        return losses

    def simple_test(self, points, img_metas, imgs=None, proposals=None, rescale=False):
        """Test function without augmentaiton."""

        # 转换成range提取特征后转回lidar
        batchsize = len(points)
        rangeImage = []
        for i in range(batchsize):
            rangeImage.append(self.lidar_to_range_gpu(points[i]).unsqueeze(0))
        rangeImage = torch.cat(rangeImage, dim=0)
        # 是否加入img信息
        # range_feat = self.range_encoder(rangeImage, img)      #用自编码器的形式
        range_feat = self.fusion(rangeImage, imgs)
        range_ori = torch.cat((rangeImage[:, 0:2], range_feat), dim=1)
        pts_with_range = []
        for i in range(batchsize):
            pts_with_range.append(self.range_to_lidar_gpu(range_ori[i].squeeze(0)))

        feats_dict, voxels_dict = self.extract_feat(pts_with_range, img_metas)

        if self.with_rpn:
            rpn_outs = self.rpn_head(feats_dict['neck_feats'])
            proposal_cfg = self.test_cfg.rpn
            bbox_inputs = rpn_outs + (img_metas, proposal_cfg)
            proposal_list = self.rpn_head.get_bboxes(*bbox_inputs)
        else:
            proposal_list = proposals

        return self.roi_head.simple_test(feats_dict, voxels_dict, img_metas,
                                         proposal_list)

    def lidar_to_range_gpu(self, points):
        device = points.device
        pi = torch.tensor(np.pi).to(device)
        fov_up = self.fov_up * pi / 180.0
        fov_down = self.fov_down * pi / 180.0
        fov = abs(fov_up) + abs(fov_down)

        depth = torch.norm(points, 2, dim=1)

        x = points[:, 0]
        y = points[:, 1]
        z = points[:, 2]

        yaw = torch.atan2(y, x)
        pitch = torch.asin(z / depth)

        u = 0.5 * (1 - 4 * yaw / pi) * self.W  # 最后一个 4 用来控制水平范围
        v = (1 - (pitch + abs(fov_down)) / fov) * self.H

        zero_tensor = torch.zeros_like(u)
        W_tensor = torch.ones_like(u) * (self.W - 1)
        H_tensor = torch.ones_like(v) * (self.H - 1)

        u = torch.floor(u)
        u = torch.min(u, W_tensor)
        u = torch.max(u, zero_tensor).long()

        v = torch.floor(v)
        v = torch.min(v, H_tensor)
        v = torch.max(v, zero_tensor).long()

        range_image = torch.full((5, self.H, self.W), 0, dtype=torch.float32).to(device)
        range_image[0][v, u] = depth
        range_image[1][v, u] = points[:, 3]
        range_image[2][v, u] = points[:, 0]
        range_image[3][v, u] = points[:, 1]
        range_image[4][v, u] = points[:, 2]
        return range_image

    def range_to_lidar_gpu(self, range_img):
        device = range_img.device
        self.uv = self.uv.to(device)
        lidar_out = torch.zeros((12, self.H, self.W)).to(device)
        lidar_out[0] = range_img[0] * torch.cos(self.uv[0]) * torch.cos(self.uv[1])
        lidar_out[1] = range_img[0] * torch.cos(self.uv[0]) * torch.sin(self.uv[1]) * (-1)
        lidar_out[2] = range_img[0] * torch.sin(self.uv[0])
        lidar_out[3:] = range_img[1:]
        lidar_out = lidar_out.permute((2, 1, 0)).reshape([-1, 12])
        lidar_out = lidar_out[torch.where(lidar_out[:, 0] != 0)]
        return lidar_out