import torch
import torch.optim
import torch.nn.functional as F
import numpy as np
from src import rotation
from torch import Tensor
from torch.nn.parameter import Parameter
import math as m

from src.utils import *
from src.hourglass_network import *

from mpl_toolkits.axes_grid1 import ImageGrid
dtype = torch.cuda.FloatTensor


def rotate_lifting_kernels(kernel, orientations_nb, periodicity=2 * np.pi, diskMask=True):
    """ Rotates the set of 2D lifting kernels.
        INPUT:
            - kernel, a tensor flow tensor with expected shape:
                [ChannelsOUT, ChannelsIN, Height, Width]
            - orientations_nb, an integer specifying the number of rotations
        INPUT (optional):
            - periodicity, rotate in total over 2*np.pi or np.pi
            - disk_mask, True or False, specifying whether or not to mask the kernels spatially
        OUTPUT:
            - set_of_rotated_kernels, a tensorflow tensor with dimensions:
                [nbOrientations, ChannelsOUT, ChannelsIN, Height, Width]
    """

    # Unpack the shape of the input kernel
    channelsOUT, channelsIN, kernelSizeH, kernelSizeW = map(int, kernel.shape)

    # Flatten the baseline kernel
    # Resulting shape: [channelsIN*channelsOUT, kernelSizeH*kernelSizeW]
    kernel_flat = torch.reshape(kernel, [channelsIN * channelsOUT, kernelSizeH * kernelSizeW])
    # Transpose: [kernelSizeH*kernelSizeW, channelsIN*channelsOUT]
    kernel_flat = torch.transpose(kernel_flat,0,1)

    # Generate a set of rotated kernels via rotation matrix multiplication
    # For efficiency purpose, the rotation matrix is implemented as a sparse matrix object
    # Result: The non-zero indices and weights of the rotation matrix
    idx, vals = rotation.MultiRotationOperatorMatrixSparse(
        [kernelSizeH, kernelSizeW],
        orientations_nb,
        periodicity=periodicity,
        diskMask=diskMask)
    
    new_idx = [[],[]]
    for i in range(len(idx)):
      new_idx[0].append(idx[i][0])
      new_idx[1].append(idx[i][1])

    # Sparse rotation matrix
    # Resulting shape: [nbOrientations*kernelSizeH*kernelSizeW,kernelSizeH*kernelSizeW]
    rotOp_matrix = torch.sparse_coo_tensor(new_idx, vals, (orientations_nb * kernelSizeH * kernelSizeW, kernelSizeH * kernelSizeW))
    # Matrix multiplication
    # Resulting shape: [nbOrientations*kernelSizeH*kernelSizeW,channelsIN*channelsOUT]
    set_of_rotated_kernels = torch.sparse.mm(rotOp_matrix.type(torch.cuda.FloatTensor), kernel_flat.type(torch.cuda.FloatTensor))

    # Reshaping
    # Resulting shape: [nbOrientations, channelsOUT, channelsIN, kernelSizeH, kernelSizeW]
    set_of_rotated_kernels = torch.reshape(set_of_rotated_kernels, [orientations_nb, kernelSizeH, kernelSizeW, channelsIN, channelsOUT])
    set_of_rotated_kernels = set_of_rotated_kernels.permute(0,4,3,1,2)
    
    return set_of_rotated_kernels





""" Constructs a group convolutional layer.
        (lifting layer from Z2 to SE2N with N input number of orientations)
        INPUT:
            - input_tensor in Z2, a tensorflow Tensor with expected shape:
                [BatchSize,  ChannelsIN, Height, Width]
            - kernel, a tensorflow Tensor with expected shape:
                [ChannelsOUT, ChannelsIN, kernelSize, kernelSize]
            - orientations_nb, an integer specifying the number of rotations
        INPUT (optional):
            - periodicity, rotate in total over 2*np.pi or np.pi
            - disk_mask, True or False, specifying whether or not to mask the kernels spatially
        OUTPUT:
            - output_tensor, the tensor after group convolutions with shape
                [BatchSize, orientations_nb, ChannelsOut, Height', Width']
                (Height', Width' are reduced sizes due to the valid convolution)
            - kernels_formatted, the formated kernels, i.e., the full stack of rotated kernels with shape:
                [orientations_nb, ChannelsOUT, ChannelsIN, kernelSize, kernelSize]
""" 

class NN_z2_se2n(nn.Module):
  def __init__(self, in_c: int, out_c: int, kernel_size: int, Ntheta: int, stride=1, padding='valid') -> None:
    super().__init__()

    self.Ntheta = Ntheta
    self.stride = stride
    self.padding = padding
    self.kernel = Parameter(torch.randn((out_c, in_c, kernel_size, kernel_size), requires_grad=True)*(m.sqrt(2/(in_c*kernel_size*kernel_size))))

  def _conv_forward(self, input_tensor: Tensor, kernel: Tensor, Ntheta: int):
    # Preparation for group convolutions
    # Precompute a rotated stack of kernels
    # Shape [nbOrientations, channelsOUT, channelsIN, kernelSizeH, kernelSizeW]
    kernel_stack = rotate_lifting_kernels(kernel, Ntheta)

    # Format the kernel stack as a 2D kernel stack (merging the rotation and channelsOUT axis)
    orientations_nb = Ntheta
    channelsOUT, channelsIN, kernelSizeH, kernelSizeW = map(int, kernel.shape)
    kernels_as_if_2D = torch.reshape(kernel_stack, [orientations_nb * channelsOUT, channelsIN, kernelSizeH, kernelSizeW])


    # Perform the 2D convolution
    # Input shape [1, channelsIN, h, w]
    # Kernels shape [nbOrientations*channelsOUT, channelsIN, kernelSizeH, kernelSizeW]
    layer_output = F.conv2d(input_tensor.type(dtype), kernels_as_if_2D.type(dtype), stride=self.stride, padding=self.padding)

    # Reshape to an SE2 image (split the orientation and channelsOUT axis)
    # Note: the batch size is unknown, hence this dimension needs to be
    # obtained using the tensorflow function tf.shape, for the other
    # dimensions we keep using tensor.shape since this allows us to keep track
    # of the actual shapes (otherwise the shapes get convert to
    # "Dimensions(None)").
    layer_output = torch.reshape(layer_output, [layer_output.shape[0], orientations_nb, channelsOUT, int(layer_output.shape[2]), int(layer_output.shape[3])])

    return layer_output
  
  def forward(self, input: Tensor) -> Tensor:
    return self._conv_forward(input, self.kernel, self.Ntheta)



def rotate_gconv_kernels(kernel, periodicity=2 * np.pi, diskMask=True):
  """ Rotates the set of SE2 kernels. 
        Rotation of SE2 kernels involves planar rotations and a shift in orientation,
        see e.g. the left-regular representation L_g of the roto-translation group on SE(2) images,
        (Eq. 3) of the MICCAI 2018 paper.
        INPUT:
            - kernel, a tensor flow tensor with expected shape:
                [orientations_nb, channelsOUT, channelsIN, kernelSizeH, kernelSizeW ]
        INPUT (optional):
            - periodicity, rotate in total over 2*np.pi or np.pi
            - disk_mask, True or False, specifying whether or not to mask the kernels spatially
        OUTPUT:
            - set_of_rotated_kernels, a tensorflow tensor with dimensions:
                [nbOrientations, ChannelsOUT, ChannelsIN, nbOrientations, Height, Width]
              I.e., for each rotation angle a rotated (shift-twisted) version of the input kernel.
  """

  # Rotation of an SE2 kernel consists of two parts:
  # PART 1. Planar rotation
  # PART 2. A shift in theta direction

  # Unpack the shape of the input kernel
  orientations_nb, channelsOUT, channelsIN, kernelSizeH, kernelSizeW = map(int, kernel.shape)

  # PART 1 (planar rotation)
  # Flatten the baseline kernel
  # Resulting shape: [orientations_nb*channelsIN*channelsOUT, kernelSizeH*kernelSizeW]
  kernel_flat = torch.reshape(kernel, [orientations_nb * channelsOUT * channelsIN, kernelSizeH * kernelSizeW])
  # Permute axis : [kernelSizeH*kernelSizeW, orientations_nb*channelsIN*channelsOUT]
  kernel_flat = torch.transpose(kernel_flat,0,1)

  # Generate a set of rotated kernels via rotation matrix multiplication
  # For efficiency purpose, the rotation matrix is implemented as a sparse matrix object
  # Result: The non-zero indices and weights of the rotation matrix
  idx, vals = rotation.MultiRotationOperatorMatrixSparse([kernelSizeH, kernelSizeW], orientations_nb, periodicity=periodicity, diskMask=diskMask)

  new_idx = [[],[]]
  for i in range(len(idx)):
    new_idx[0].append(idx[i][0])
    new_idx[1].append(idx[i][1])

  # The corresponding sparse rotation matrix
  # Resulting shape: [nbOrientations*kernelSizeH*kernelSizeW,kernelSizeH*kernelSizeW]
  rotOp_matrix = torch.sparse_coo_tensor(new_idx, vals, [orientations_nb * kernelSizeH * kernelSizeW, kernelSizeH * kernelSizeW])

  # Matrix multiplication (each 2D plane is now rotated)
  # Resulting shape: [nbOrientations*kernelSizeH*kernelSizeW, orientations_nb*channelsIN*channelsOUT]
  kernels_planar_rotated = torch.sparse.mm(rotOp_matrix.type(torch.cuda.FloatTensor), kernel_flat.type(torch.cuda.FloatTensor))
  kernels_planar_rotated = torch.reshape(kernels_planar_rotated, [orientations_nb, kernelSizeH, kernelSizeW, orientations_nb, channelsOUT, channelsIN])
  # Resulting shape: [orientations_nb,channelsOUT,channelsIN,orientations_nb,kernelSizeH,kernelSizeW]
  kernels_planar_rotated = kernels_planar_rotated.permute(3,4,5,0,1,2)

  # PART 2 (shift in theta direction)
  set_of_rotated_kernels = [None] * orientations_nb
  for orientation in range(orientations_nb):
      # [orientations_nb,channelsOUT,channelsIN,kernelSizeH,kernelSizeW]
      kernels_temp = kernels_planar_rotated[:,:,:,orientation,:,:]
      # [channelsOUT,channelsIN,kernelSizeH,kernelSizeW,orientations_nb]
      kernels_temp = kernels_temp.permute(1, 2, 3, 4, 0)
      # [kernelSizeH*kernelSizeW*channelsIN*channelsOUT*orientations_nb]
      kernels_temp = torch.reshape(kernels_temp, [kernelSizeH * kernelSizeW * channelsIN * channelsOUT, orientations_nb])
      # Roll along the orientation axis
      roll_matrix = torch.tensor(np.roll(np.identity(orientations_nb), orientation, axis=1), dtype=torch.float32)
      kernels_temp = torch.matmul(kernels_temp.type(torch.cuda.FloatTensor), roll_matrix.type(torch.cuda.FloatTensor))
      kernels_temp = torch.reshape(kernels_temp, [channelsOUT, channelsIN, kernelSizeH, kernelSizeW, orientations_nb])
      kernels_temp = kernels_temp.permute(0,1,4,2,3)
      set_of_rotated_kernels[orientation] = kernels_temp

  return torch.stack(set_of_rotated_kernels)






""" Constructs a group convolutional layer.
        (group convolution layer from SE2N to SE2N with N input number of orientations)
        INPUT:
            - input_tensor in SE2n, a tensor flow tensor with expected shape:
                [BatchSize, nbOrientations, ChannelsIN, Height, Width]
            - kernel, a tensorflow Tensor with expected shape:
                [orientations_nb, channelsOUT, channelsIN, kernelSizeH, kernelSizeW ]
        INPUT (optional):
            - periodicity, rotate in total over 2*np.pi or np.pi
            - disk_mask, True or False, specifying whether or not to mask the
                kernels spatially
        OUTPUT:
            - output_tensor, the tensor after group convolutions with shape
                [BatchSize, nbOrientations, ChannelsOut, Height', Width']
                (Height', Width' are the reduced sizes due to the valid convolution)
"""

class NN_se2n_se2n(nn.Module):
  def __init__(self, in_c: int, out_c: int, kernel_size: int, Ntheta: int, stride=1, padding='valid') -> None:
    super().__init__()

    self.stride = stride
    self.padding = padding
    self.kernel = Parameter(torch.randn((Ntheta, out_c, in_c, kernel_size, kernel_size), requires_grad=True)*(m.sqrt(2/(in_c*kernel_size*kernel_size))))

  def _conv_forward(self, input_tensor: Tensor, kernel: Tensor):
    # Kernel dimensions
    orientations_nb, channelsOUT, channelsIN, kernelSizeH, kernelSizeW = map(int, kernel.shape)

    # Preparation for group convolutions
    # Precompute a rotated stack of se2 kernels
    # With shape: [orientations_nb, channelsOUT, channelsIN, orientations_nb, kernelSizeH, kernelSizeW]
    kernel_stack = rotate_gconv_kernels(kernel)

    # Group convolutions are done by integrating over [x,y,theta,input-channels] for each translation and rotation of the kernel
    # We compute this integral by doing standard 2D convolutions (translation part) for each rotated version of the kernel (rotation part)
    # In order to efficiently do this we use 2D convolutions where the theta
    # and input-channel axes are merged (thus treating the SE2 image as a 2D
    # feature map)

    # Prepare the input tensor (merge the orientation and channel axis) for
    # the 2D convolutions:
    input_tensor_as_if_2D = torch.reshape(input_tensor, [input_tensor.shape[0], orientations_nb * channelsIN, int(input_tensor.shape[3]), int(input_tensor.shape[4])])

    # Reshape the kernels for 2D convolutions (orientation+channelsIN axis are
    # merged, rotation+channelsOUT axis are merged)
    kernels_as_if_2D = torch.reshape(kernel_stack, [orientations_nb * channelsOUT, orientations_nb * channelsIN, kernelSizeH, kernelSizeW])

    # Perform the 2D convolutions
    layer_output = F.conv2d(input_tensor_as_if_2D.type(torch.cuda.FloatTensor), kernels_as_if_2D.type(torch.cuda.FloatTensor), stride=self.stride, padding=self.padding)

    # Reshape into an SE2 image (split the orientation and channelsOUT axis)
    layer_output = torch.reshape(layer_output, [layer_output.shape[0],  orientations_nb, channelsOUT, int(layer_output.shape[2]), int(layer_output.shape[3])])

    return layer_output

  def forward(self, input: Tensor) -> Tensor:
    return self._conv_forward(input, self.kernel)




class roto_decoder_block(nn.Module):
  def __init__(self, in_c, out_c, kernel_size, Ntheta, up_sampling_mode):
    super().__init__()

    self.Ntheta = Ntheta
    self.out = out_c
    self.bn = nn.BatchNorm2d(out_c*Ntheta)
    self.bn2 = nn.BatchNorm2d(out_c)
    self.lifting = NN_z2_se2n(in_c, out_c, kernel_size, Ntheta, 1, 'same')
    self.relu = nn.LeakyReLU(0.2, inplace=True)
    self.conv = NN_se2n_se2n(out_c, out_c, kernel_size, Ntheta, 1, 'same')
    #self.pool = NN_spatial_max_pool(Ntheta, in_c)
    self.up = nn.Upsample(scale_factor=2, mode=up_sampling_mode)

  def forward(self, inputs):

    x = self.up(inputs)
    x = self.lifting(x)
    x = torch.reshape(x, [x.shape[0], x.shape[1]*x.shape[2], x.shape[3], x.shape[4]])
    x = self.bn(x)
    x = self.relu(x)
    x = torch.reshape(x, [x.shape[0], self.Ntheta, self.out, x.shape[2], x.shape[3]])

    x = self.conv(x)
    x = torch.reshape(x, [x.shape[0], x.shape[1]*x.shape[2], x.shape[3], x.shape[4]])
    x = self.bn(x)
    x = self.relu(x)
    x = torch.reshape(x, [x.shape[0], self.Ntheta, self.out, x.shape[2], x.shape[3]])

    #x = self.pool(x)
    x, _ = torch.max(x,1)
    x = self.bn2(x)
    x = self.relu(x)

    return x



class roto_decoder_skip_block(nn.Module):
  def __init__(self, in_c, out_c, kernel_size, Ntheta, up_sampling_mode):
    super().__init__()

    self.Ntheta = Ntheta
    self.out = out_c
    self.bn = nn.BatchNorm2d(out_c*Ntheta)
    self.bn2 = nn.BatchNorm2d(out_c)
    self.lifting = NN_z2_se2n(in_c, out_c, kernel_size, Ntheta, 1, 'same')
    self.relu = nn.LeakyReLU(0.2, inplace=True)
    self.conv = NN_se2n_se2n(out_c, out_c, kernel_size, Ntheta, 1, 'same')
    #self.pool = NN_spatial_max_pool(Ntheta, in_c)
    self.up = nn.Upsample(scale_factor=2, mode=up_sampling_mode)

  def forward(self, inputs, skip):

    x = torch.cat([inputs, skip], axis=1)
    x = self.up(x)
    x = self.lifting(x)
    x = torch.reshape(x, [x.shape[0], x.shape[1]*x.shape[2], x.shape[3], x.shape[4]])
    x = self.bn(x)
    x = self.relu(x)
    x = torch.reshape(x, [x.shape[0], self.Ntheta, self.out, x.shape[2], x.shape[3]])

    x = self.conv(x)
    x = torch.reshape(x, [x.shape[0], x.shape[1]*x.shape[2], x.shape[3], x.shape[4]])
    x = self.bn(x)
    x = self.relu(x)
    x = torch.reshape(x, [x.shape[0], self.Ntheta, self.out, x.shape[2], x.shape[3]])

    #x = self.pool(x)
    x, _ = torch.max(x,1)
    x = self.bn2(x)
    x = self.relu(x)

    return x



class roto_encoder_block(nn.Module):
  def __init__(self, in_c, out_c, kernel_size, Ntheta):
    super().__init__()

    self.Ntheta = Ntheta
    self.out = out_c
    self.bn = nn.BatchNorm2d(out_c*Ntheta)
    self.bn2 = nn.BatchNorm2d(out_c)
    self.lifting = NN_z2_se2n(in_c, out_c, kernel_size, Ntheta, 1, 'same')
    self.relu = nn.LeakyReLU(0.2, inplace=True)
    self.conv = NN_se2n_se2n(out_c, out_c, kernel_size, Ntheta, 1, 'same')
    #self.pool = NN_spatial_max_pool(Ntheta, in_c)
    self.maxpool = torch.nn.MaxPool2d(2)

  def forward(self, inputs):

    x = self.lifting(inputs)
    x = torch.reshape(x, [x.shape[0], x.shape[1]*x.shape[2], x.shape[3], x.shape[4]])
    x = self.bn(x)
    x = self.relu(x)
    x = torch.reshape(x, [x.shape[0], self.Ntheta, self.out, x.shape[2], x.shape[3]])

    x = self.conv(x)
    x = torch.reshape(x, [x.shape[0], x.shape[1]*x.shape[2], x.shape[3], x.shape[4]])
    x = self.bn(x)
    x = self.relu(x)
    x = torch.reshape(x, [x.shape[0], self.Ntheta, self.out, x.shape[2], x.shape[3]])

    #x = self.pool(x)
    x, _ = torch.max(x,1)
    x = self.relu(x)
    x = self.maxpool(x)
    x = self.bn2(x)
    x = self.relu(x)

    return x
