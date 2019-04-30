# -*- coding: utf-8 -*-

"""
Functions used to format and clean any input loaded in bigfish.
"""

import os
import warnings

import numpy as np
import pandas as pd

from .loader import read_tif, read_cell_json, read_rna_json
from .utils import check_array, check_range_value

from sklearn.preprocessing import LabelEncoder

from skimage import img_as_ubyte, img_as_float32, img_as_float64, img_as_uint
from skimage.morphology.selem import square, diamond, rectangle, disk
from skimage.filters import rank, gaussian
from skimage.exposure import rescale_intensity

from scipy.ndimage import gaussian_laplace
from scipy.sparse import coo_matrix

from scipy import ndimage as ndi


# TODO add safety checks
# TODO add a stack builder without recipe

# ### Simulated data ###

def build_simulated_dataset(path_cell, path_rna, path_output=None):
    """Build a dataset from the simulated coordinates of the nucleus, the
    cytoplasm and the RNA.

    Parameters
    ----------
    path_cell : str
        Path of the json file with the 2D nucleus and cytoplasm coordinates
        used by FishQuant to simulate the data.
    path_rna : str
        Path of the json file with the 3D RNA localization simulated by
        FishQuant. If it is the path of a folder, all its json files will be
        aggregated.
    path_output : str
        Path of the output file with the merged dataset. The final dataframe is
        serialized and store in a pickle file.

    Returns
    -------
    df : pandas.DataFrame
        Dataframe with all the simulated cells, the coordinates of their
        different elements and the localization pattern used to simulate them.
    df_cell : pandas.DataFrame
        Dataframe with the 2D coordinates of the nucleus and the cytoplasm of
        actual cells used to simulate data.
    df_rna : pandas.DataFrame
        Dataframe with 3D coordinates of the simulated RNA, localization
        pattern used to simulate them and its strength.

    """
    # read the cell data (nucleus + cytoplasm)
    df_cell = read_cell_json(path_cell)

    # read the RNA data
    if os.path.isdir(path_rna):
        # we concatenate all the json file in the folder
        simulations = []
        for filename in os.listdir(path_rna):
            if ".json" in filename:
                path = os.path.join(path_rna, filename)
                df_ = read_rna_json(path)
                simulations.append(df_)
        df_rna = pd.concat(simulations)
        df_rna.reset_index(drop=True, inplace=True)

    else:
        # we directly read the json file
        df_rna = read_rna_json(path_rna)

    # merge the dataframe
    df = pd.merge(df_rna, df_cell, on="name_img_BGD")

    # save output
    if path_output is not None:
        df.to_pickle(path_output)

    return df, df_cell, df_rna


# ### Real data ###

def build_stacks(data_map, input_dimension=None, normalize=False,
                 channel_to_stretch=None, stretching_percentile=99.9,
                 cast_8bit=False, return_origin=False):
    """Generator to build several stacks.

    To build a stack, a recipe should be linked to a directory including all
    the files needed to build the stack. The content of the recipe allows to
    reorganize the different files stored in the directory in order to build
    a 5-d tensor.

    The list 'data_map' takes the form:

        [
         (recipe_1, path_input_directory_1),
         (recipe_2, path_input_directory_1),
         (recipe_3, path_input_directory_1),
         (recipe_4, path_input_directory_2),
         ...
        ]

    The recipe dictionary for one field of view takes the form:

        {
         "fov": str,
         "z": List[str], (optional)
         "c": List[str], (optional)
         "r": List[str], (optional)
         "ext": str
         }

    - A field of view is defined by an ID common to every images belonging to
    the field of view ("fov").
    - At least every images are in 2-d with x and y dimensions. So we need to
    mention the round-dimension, the channel-dimension and the z-dimension to
    add ("r", "c" and "z"). For these keys, we provide a list of
    strings to identify the images to stack. By default, we assume the filename
    fit the pattern fov_z_c_r.tif.
    - An extra information to identify the files to stack in the input folder
    can be provided with the file extension "ext" (usually 'tif' or 'tiff').

    For example, let us assume 3-d images (zyx dimensions) saved as
    "r03c03f01_405.tif", "r03c03f01_488.tif" and "r03c03f01_561.tif". The first
    morpheme "r03c03f01" uniquely identifies a 3-d field of view. The second
    morphemes "405", "488" and "561" identify three different channels we
    want to stack. There is no round in this experiment. Thus, the recipe is:

        {
         "fov": "r03c03f01",
         "c": ["405", "488", "561"],
         "ext": "tif"
         }

    The function should return a tensor with shape (1, 3, z, y, x).

    Parameters
    ----------
    data_map : List[tuple]
        Map between input directories and recipes.
    input_dimension : str
        Number of dimensions of the loaded files.
    normalize : bool
        Normalize the different channels of the loaded stack (rescaling).
    channel_to_stretch : int or List[int]
        Channel to stretch.
    stretching_percentile : float
        Percentile to determine the maximum intensity value used to rescale
        the image.
    return_origin : bool
        Return the input directory and the recipe used to build the stack.
    cast_8bit : bool
        Cast tensor in np.uint8.

    Returns
    -------
    tensor : np.ndarray, np.uint
        Tensor with shape (r, c, z, y, x).
    input_directory : str
        Path of the input directory from where the tensor is built.
    recipe : dict
        Recipe used to build the tensor.

    """
    # load and generate tensors
    for recipe, input_folder in data_map:
        tensor = build_stack(recipe, input_folder, input_dimension, normalize,
                             channel_to_stretch, stretching_percentile,
                             cast_8bit)
        if return_origin:
            yield tensor, input_folder, recipe
        else:
            yield tensor


def build_stack(recipe, input_folder, input_dimension=None, normalize=False,
                channel_to_stretch=None, stretching_percentile=99.9,
                cast_8bit=False):
    """Build 5-d stack and normalize it.

    The recipe dictionary for one field of view takes the form:

        {
         "fov": str,
         "z": List[str], (optional)
         "c": List[str], (optional)
         "r": List[str], (optional)
         "ext": str
         }

    - A field of view is defined by an ID common to every images belonging to
    the field of view ("fov").
    - At least every images are in 2-d with x and y dimensions. So we need to
    mention the round-dimension, the channel-dimension and the z-dimension to
    add ("r", "c" and "z"). For these keys, we provide a list of
    strings to identify the images to stack. By default, we assume the filename
    fit the pattern fov_z_c_r.tif.
    - An extra information to identify the files to stack in the input folder
    can be provided with the file extension "ext" (usually 'tif' or 'tiff').

    For example, let us assume 3-d images (zyx dimensions) saved as
    "r03c03f01_405.tif", "r03c03f01_488.tif" and "r03c03f01_561.tif". The first
    morpheme "r03c03f01" uniquely identifies a 3-d field of view. The second
    morphemes "405", "488" and "561" identify three different channels we
    want to stack. There is no round in this experiment. Thus, the recipe is:

        {
         "fov": "r03c03f01",
         "c": ["405", "488", "561"],
         "ext": "tif"
         }

    The function should return a tensor with shape (1, 3, z, y, x).

    Parameters
    ----------
    recipe : dict
        Map the images according to their field of view, their round,
        their channel and their spatial dimensions.
    input_folder : str
        Path of the folder containing the images.
    input_dimension : str
        Number of dimensions of the loaded files.
    normalize : bool
        Normalize the different channels of the loaded stack (rescaling).
    channel_to_stretch : int or List[int]
        Channel to stretch.
    stretching_percentile : float
        Percentile to determine the maximum intensity value used to rescale
        the image.
    cast_8bit : bool
        Cast the tensor in np.uint8.

    Returns
    -------
    tensor : np.ndarray, np.uint
        Tensor with shape (r, c, z, y, x).

    """
    # TODO add sanity checks for the parameters
    # TODO ensure we can pass a str and not just a list of str in the recipe
    # TODO allow different patterns for the recipe
    # build stack from recipe and tif files
    tensor = load_stack(recipe, input_folder, input_dimension)

    # rescale data and improve contrast
    if normalize:
        tensor = rescale(tensor, channel_to_stretch, stretching_percentile)

    # cast in np.uint8 if necessary, in order to reduce memory allocation
    if tensor.dtype == np.uint16 and cast_8bit:
        tensor = cast_img_uint8(tensor)

    return tensor


def load_stack(recipe, input_folder, input_dimension=None):
    """Build a 5-d tensor from the same field of view (fov).

    The function stacks a set of images using a recipe mapping the
    different images with the dimensions they represent. Each stacking step
    add a new dimension to the original tensors (eg. we stack 2-d images with
    the same xy coordinates, but different depths to get a 3-d image). If the
    files we need to build a new dimension are not included in the
    recipe, an empty dimension is added. This operation is repeated until we
    get a 5-d tensor. We first operate on the z dimension, then the
    channels and eventually the rounds.

    The recipe dictionary for one field of view takes the form:

        {
         "fov": str,
         "z": List[str], (optional)
         "c": List[str], (optional)
         "r": List[str], (optional)
         "ext": str
         }

    - A field of view is defined by an ID common to every images belonging to
    the field of view ("fov").
    - At least every images are in 2-d with x and y dimensions. So we need to
    mention the round-dimension, the channel-dimension and the z-dimension to
    add ("r", "c" and "z"). For these keys, we provide a list of
    strings to identify the images to stack. By default, we assume the filename
    fit the pattern fov_z_c_r.tif.
    - An extra information to identify the files to stack in the input folder
    can be provided with the file extension "ext" (usually 'tif' or 'tiff').

    # TODO generalize with different filename patterns
    # TODO allow a recipe without 'ext'

    For example, let us assume 3-d images (zyx dimensions) saved as
    "r03c03f01_405.tif", "r03c03f01_488.tif" and "r03c03f01_561.tif". The first
    morpheme "r03c03f01" uniquely identifies a 3-d field of view. The second
    morphemes "405", "488" and "561" identify three different channels we
    want to stack. There is no round in this experiment. Thus, the recipe is:

        {
         "fov": "r03c03f01",
         "c": ["405", "488", "561"],
         "ext": "tif"
         }

    The function should return a tensor with shape (1, 3, z, y, x).

    # TODO manage the order of the channel

    Parameters
    ----------
    recipe : dict
        Map the images according to their field of view, their round,
        their channel and their spatial dimensions.
    input_folder : str
        Path of the folder containing the images.
    input_dimension : str
        Number of dimensions of the loaded files.

    Returns
    -------
    tensor : np.ndarray, np.uint
        Tensor with shape (r, c, z, y, x).

    """
    # check recipe
    check_recipe(recipe)

    # if the initial dimension of the files is unknown, we read one of them
    # TODO be sure to read one of the files targeted by the recipe
    if input_dimension is None:
        fov_str = recipe["fov"]
        ext_str = "." + recipe["ext"]
        filenames = [filename
                     for filename in os.listdir(input_folder)
                     if fov_str in filename and ext_str in filename]
        path = os.path.join(input_folder, filenames[0])
        testfile = read_tif(path)
        input_dimension = testfile.ndim

    # we stack our files according to their initial dimension
    if input_dimension == 2:
        stack = _build_stack_from_2d(recipe, input_folder)
    elif input_dimension == 3:
        stack = _build_stack_from_3d(recipe, input_folder)
    elif input_dimension == 4:
        stack = _build_stack_from_4d(recipe, input_folder)
    elif input_dimension == 5:
        stack = _build_stack_from_5d(recipe, input_folder)
    else:
        raise ValueError("Files do not have the right number of dimensions: "
                         "{0}. The files we stack should be in 2-d, 3-d, 4-d "
                         "or 5-d.".format(input_dimension))

    return stack


def check_recipe(recipe):
    """Check and validate a recipe.

    Parameters
    ----------
    recipe : dict
        Map the images according to their field of view, their round,
        their channel and their spatial dimensions.

    Returns
    -------
    expected_dimension : int
        The number of dimensions expected in the tensors used with this
        recipe.

    """
    # TODO remove the expected dimension ?
    # check recipe is a dictionary with the "fov" key
    if (not isinstance(recipe, dict)
            or "fov" not in recipe
            or "ext" not in recipe):
        raise Exception("The recipe is not valid.")

    # determine the minimum number of dimensions expected for the tensors
    if ("r" in recipe and isinstance(recipe["r"], list)
            and len(recipe["r"]) > 0):
        return 4
    if ("c" in recipe and isinstance(recipe["c"], list)
            and len(recipe["c"]) > 0):
        return 3
    if ("z" in recipe and isinstance(recipe["z"], list)
            and len(recipe["z"]) > 0):
        return 2
    raise Exception("The recipe is not valid.")


def _extract_recipe(recipe):
    """Extract morphemes from the recipe to correctly stack the files.

    Parameters
    ----------
    recipe : dict
        Map the images according to their field of view, their round,
        their channel and their spatial dimensions.

    Returns
    -------
    l_round : List[str]
        List of morphemes used to catch the files from the right round.
    l_channel : List[str]
        List of morphemes used to catch the files from the right channel.
    l_z : List[str]
        List of morphemes used to catch the files from the right z.

    """
    # we collect the different morphemes we use to identify the images
    if ("r" in recipe
            and isinstance(recipe["r"], list)
            and len(recipe["r"]) > 0):
        l_round = recipe["r"]
    else:
        l_round = [""]

    if ("c" in recipe
            and isinstance(recipe["c"], list)
            and len(recipe["c"]) > 0):
        l_channel = recipe["c"]
    else:
        l_channel = [""]

    if ("z" in recipe
            and isinstance(recipe["z"], list)
            and len(recipe["z"]) > 0):
        l_z = recipe["z"]
    else:
        l_z = [""]

    return l_round, l_channel, l_z


def _build_stack_from_2d(recipe, input_folder):
    """Load and stack 2-d tensors.

    Parameters
    ----------
    recipe : dict
        Map the images according to their field of view, their round,
        their channel and their spatial dimensions.
    input_folder : str
        Path of the folder containing the images.

    Returns
    -------
    tensor_5d : np.ndarray, np.uint
        Tensor with shape (r, c, z, y, x).

    """
    # check we can find the tensors to stack from the recipe
    l_round, l_channel, l_z = _extract_recipe(recipe)

    # stack images from the same fov
    fov_str = recipe["fov"]
    ext_str = "." + recipe["ext"]

    # stack 4-d tensors in 5-d
    tensors_4d = []
    for round_str in l_round:
        if round_str != "":
            round_str = "_" + round_str

        # stack 3-d tensors in 4-d
        tensors_3d = []
        for channel_str in l_channel:
            if channel_str != "":
                channel_str = "_" + channel_str

            # stack 2-d tensors in 3-d
            tensors_2d = []
            for z_str in l_z:
                if z_str != "":
                    z_str = "_" + z_str
                filename = fov_str + z_str + channel_str + round_str + ext_str
                path = os.path.join(input_folder, filename)
                tensor_2d = read_tif(path)
                tensors_2d.append(tensor_2d)
            tensor_3d = np.stack(tensors_2d, axis=0)
            tensors_3d.append(tensor_3d)

        tensor_4d = np.stack(tensors_3d, axis=0)
        tensors_4d.append(tensor_4d)

    tensor_5d = np.stack(tensors_4d, axis=0)

    return tensor_5d


def _build_stack_from_3d(recipe, input_folder):
    """Load and stack 3-d tensors.

    Parameters
    ----------
    recipe : dict
        Map the images according to their field of view, their round,
        their channel and their spatial dimensions.
    input_folder : str
        Path of the folder containing the images.

    Returns
    -------
    tensor_5d : np.ndarray, np.uint
        Tensor with shape (r, c, z, y, x).

    """
    # check we can find the tensors to stack from the recipe
    l_round, l_channel, l_z = _extract_recipe(recipe)

    # stack images from the same fov
    fov_str = recipe["fov"]
    ext_str = "." + recipe["ext"]

    # stack 4-d tensors in 5-d
    tensors_4d = []
    for round_str in l_round:
        if round_str != "":
            round_str = "_" + round_str

        # stack 3-d tensors in 4-d
        tensors_3d = []
        for channel_str in l_channel:
            if channel_str != "":
                channel_str = "_" + channel_str
            filename = fov_str + channel_str + round_str + ext_str
            path = os.path.join(input_folder, filename)
            tensor_3d = read_tif(path)
            tensors_3d.append(tensor_3d)
        tensor_4d = np.stack(tensors_3d, axis=0)
        tensors_4d.append(tensor_4d)

    tensor_5d = np.stack(tensors_4d, axis=0)

    return tensor_5d


def _build_stack_from_4d(recipe, input_folder):
    """Load and stack 4-d tensors.

    Parameters
    ----------
    recipe : dict
        Map the images according to their field of view, their round,
        their channel and their spatial dimensions.
    input_folder : str
        Path of the folder containing the images.

    Returns
    -------
    tensor_5d : np.ndarray, np.uint
        Tensor with shape (r, c, z, y, x).

    """
    # check we can find the tensors to stack from the recipe
    l_round, l_channel, l_z = _extract_recipe(recipe)

    # stack images from the same fov
    fov_str = recipe["fov"]
    ext_str = "." + recipe["ext"]

    # stack 4-d tensors in 5-d
    tensors_4d = []
    for round_str in l_round:
        if round_str != "":
            round_str = "_" + round_str
        filename = fov_str + round_str + ext_str
        path = os.path.join(input_folder, filename)
        tensor_4d = read_tif(path)
        tensors_4d.append(tensor_4d)
    tensor_5d = np.stack(tensors_4d, axis=0)

    return tensor_5d


def _build_stack_from_5d(recipe, input_folder):
    """Load directly a 5-d tensor.

    Parameters
    ----------
    recipe : dict
        Map the images according to their field of view, their round,
        their channel and their spatial dimensions.
    input_folder : str
        Path of the folder containing the images.

    Returns
    -------
    tensor_5d : np.ndarray, np.uint
        Tensor with shape (r, c, z, y, x).

    """
    # stack the images
    fov_str = recipe["fov"]
    ext_str = "." + recipe["ext"]
    filename = fov_str + ext_str
    path = os.path.join(input_folder, filename)
    tensor_5d = read_tif(path)

    return tensor_5d


# ### Projections 2-d ###

def projection(tensor, method="mip", r=0, c=0):
    """ Project a tensor along the z-dimension.

    Parameters
    ----------
    tensor : np.ndarray, np.uint
        A 5-d tensor with shape (r, c, z, y, x).
    method : str
        Method used to project ('mip', 'focus').
    r : int
        Index of a specific round to project.
    c : int
        Index of a specific channel to project.

    Returns
    -------
    projected_tensor : np.ndarray
        A 2-d tensor with shape (y, x).

    """
    # check tensor dimensions and its dtype
    check_array(tensor, ndim=5, dtype=[np.uint8, np.uint16])

    # apply projection along the z-dimension
    projected_tensor = tensor[r, c, :, :, :]
    if method == "mip":
        projected_tensor = maximum_projection(projected_tensor)
    elif method == "mean":
        projected_tensor = mean_projection(projected_tensor)
    elif method == "median":
        projected_tensor = median_projection(projected_tensor)
    elif method == "focus":
        # TODO complete focus projection with different strategies
        raise ValueError("Focus projection is not implemented yet.")

    return projected_tensor


def maximum_projection(tensor):
    """Project the z-dimension of a tensor, keeping the maximum intensity of
    each yx pixel.

    Parameters
    ----------
    tensor : np.ndarray, np.uint
        A 3-d tensor with shape (z, y, x).

    Returns
    -------
    projected_tensor : np.ndarray, np.uint
        A 2-d tensor with shape (y, x).

    """
    # project tensor along the z axis
    projected_tensor = tensor.max(axis=0, keepdims=True)

    return projected_tensor[0]


def mean_projection(tensor):
    """Project the z-dimension of a tensor, computing the mean intensity of
    each yx pixel.

    Parameters
    ----------
    tensor : np.ndarray, np.uint
        A 3-d tensor with shape (z, y, x).

    Returns
    -------
    projected_tensor : np.ndarray, np.float
        A 2-d tensor with shape (y, x).

    """
    # project tensor along the z axis
    projected_tensor = tensor.mean(axis=0, keepdims=True)

    return projected_tensor[0]


def median_projection(tensor):
    """Project the z-dimension of a tensor, computing the median intensity of
    each yx pixel.

    Parameters
    ----------
    tensor : np.ndarray, np.uint
        A 3-d tensor with shape (z, y, x).

    Returns
    -------
    projected_tensor : np.ndarray, np.uint
        A 2-d tensor with shape (y, x).

    """
    # project tensor along the z axis
    projected_tensor = tensor.median(axis=0, keepdims=True)

    return projected_tensor[0]


def focus_projection(tensor, channel=0, p=0.75, global_neighborhood_size=30,
                     method="best"):
    """

    Parameters
    ----------
    tensor
    channel
    p
    global_neighborhood_size
    method

    Returns
    -------

    """

    # get 3-d image
    image = tensor[0, channel, :, :, :]

    # measure global focus level for each z-slices
    ratio, l_focus = focus_measurement_3d(image, global_neighborhood_size)

    # remove out-of-focus slices
    indices_to_keep = get_in_focus(l_focus, p)
    in_focus_image = image[indices_to_keep]

    projected_image = None
    if method == "bast":
        # for each pixel, we project the z-slice value with the highest focus
        ratio_2d = np.argmax(ratio[indices_to_keep], axis=0)
        one_hot = one_hot_3d(ratio_2d, depth=len(indices_to_keep))
        projected_image = np.multiply(in_focus_image, one_hot).max(axis=0)
    elif method == "median":
        # for each pixel, we compute the median value of the in-focus z-slices
        projected_image = np.median(in_focus_image, axis=0)
    elif method == "mean":
        # for each pixel, we compute the mean value of the in-focus z-slices
        projected_image = np.median(in_focus_image, axis=0)

    return projected_image, ratio, l_focus


def focus_measurement_2d(image, neighborhood_size):
    """Helmli and Scherer’s mean method used as a focus metric.

    For each pixel xy in an image, we compute the ratio:

        R(x, y) = mu(x, y) / I(x, y), if mu(x, y) >= I(x, y)

    or

        R(x, y) = I(x, y) / mu(x, y), otherwise

    with I(x, y) the intensity of the pixel xy and mu(x, y) the mean intensity
    of the pixels of its neighborhood.

    Parameters
    ----------
    image : np.ndarray, np.float32
        A 2-d tensor with shape (y, x).
    neighborhood_size : int
        The size of the square used to define the neighborhood of each pixel.

    Returns
    -------
    global_focus : np.float32
        Mean value of the ratio computed for every pixels of the image. Can be
        used as a metric to quantify the focus level of an 2-d image.
    ratio : np.ndarray, np.float32
        A 2-d tensor with the R(x, y) computed for each pixel of the original
        image.
    image_filtered_mean : np.ndarray, np.float32
        A 2-d tensor with shape (y, x).

    """

    # scikit-image filter use np.uint dtype (so we cast to np.uint8)
    image_2d = img_as_ubyte(image)

    # filter the image with a mean filter
    selem = square(neighborhood_size)
    image_filtered_mean = rank.mean(image_2d, selem)

    # cast again in np.float32
    image_2d = img_as_float32(image_2d)
    image_filtered_mean = img_as_float32(image_filtered_mean)

    # case where mu(x, y) >= I(x, y)
    mask_1 = image_2d != 0
    out_1 = np.zeros_like(image_filtered_mean, dtype=np.float32)
    ratio_1 = np.divide(image_filtered_mean, image_2d, out=out_1, where=mask_1)
    ratio_1 = np.where(image_filtered_mean >= image_2d, ratio_1, 0)

    # case where I(x, y) > mu(x, y)
    mask_2 = image_filtered_mean != 0
    out_2 = np.zeros_like(image_2d, dtype=np.float32)
    ratio_2 = np.divide(image_2d, image_filtered_mean, out=out_2, where=mask_2)
    ratio_2 = np.where(image_2d > image_filtered_mean, ratio_2, 0)

    # compute ratio and global focus for the entire image
    ratio = ratio_1 + ratio_2
    global_focus = ratio.mean()

    return global_focus, ratio, image_filtered_mean


def focus_measurement_3d(image, neighborhood_size):
    """Helmli and Scherer’s mean method used as a focus metric.

    Parameters
    ----------
    image : np.ndarray, np.float32
        A 3-d tensor with shape (z, y, x).
    neighborhood_size : int
        The size of the square used to define the neighborhood of each pixel.

    Returns
    -------
    ratio : np.ndarray, np.float32
        A 3-d tensor with the R(x, y) computed for each pixel of the original
        3-d image, for each z-slice.
    l_focus : list
        List of the global focus computed for each z-slice.

    """
    # apply focus_measurement_2d for each z-slice
    l_ratio = []
    l_focus = []
    for z in range(image.shape[0]):
        focus, ratio_2d, _ = focus_measurement_2d(image[z], neighborhood_size)
        l_ratio.append(ratio_2d)
        l_focus.append(focus)

    # get 3-d Helmli and Scherer’s ratio
    ratio = np.stack(l_ratio)

    return ratio, l_focus


def get_in_focus(l_focus, proportion):
    """ Select the best in-focus z-slices.

    Parameters
    ----------
    l_focus : array_like
        List of the global focus computed for each z-slice.
    proportion : float or int
        Proportion of z-slices to keep (float between 0 and 1) or number of
        z-slices to keep (integer above 1).

    Returns
    -------
    indices_to_keep : np.array
    """
    # get the number of z-slices to keep
    if proportion < 1 and isinstance(proportion, float):
        n = int(len(l_focus) * proportion)
    else:
        n = int(proportion)

    # select the best z-slices
    indices_to_keep = np.argsort(l_focus)[-n:]

    return indices_to_keep


def one_hot_3d(tensor_2d, depth):
    """Build a 3-d one-hot matrix from a 2-d indices matrix.

    Parameters
    ----------
    tensor_2d : np.ndarray, int
        A 2-d tensor with integer indices and shape (y, x).
    depth : int
        Depth of the 3-d one-hot matrix.

    Returns
    -------
    one_hot : np.ndarray, np.uint8
        A 3-d binary tensor with shape (depth, y, x)

    """
    # initialize the 3-d one-hot matrix
    one_hot = np.zeros((tensor_2d.size, depth), dtype=np.uint8)

    # flatten the matrix to easily one-hot encode it, then reshape it
    one_hot[np.arange(tensor_2d.size), tensor_2d.ravel()] = 1
    one_hot.shape = tensor_2d.shape + (depth,)

    # rearrange the axis
    one_hot = np.moveaxis(one_hot, source=2, destination=0)

    return one_hot


# ### Normalization ###

def rescale(tensor, channel_to_stretch=None, stretching_percentile=99.9):
    """Rescale tensor values up to its dtype range.

    Each round and each channel is rescaled independently.

    We can improve the contrast of the image by stretching its range of
    intensity values. To do that we provide a smaller range of pixel intensity
    to rescale, spreading out the information contained in the original
    histogram. Usually, we apply such normalization to smFish channels. Other
    channels are simply rescale from the minimum and maximum intensity values
    of the image to those of its dtype.

    Parameters
    ----------
    tensor : np.ndarray, np.uint
        Tensor to rescale with shape (r, c, z, y, x).
    channel_to_stretch : int or List[int]
        Channel to stretch.
    stretching_percentile : float
        Percentile to determine the maximum intensity value used to rescale
        the image.

    Returns
    -------
    tensor : np.ndarray, np.uint
        Tensor to rescale with shape (r, c, z, y, x).

    """
    # check tensor dtype
    check_array(tensor, ndim=5, dtype=[np.uint8, np.uint16])

    # format 'channel_to_stretch'
    if channel_to_stretch is None:
        channel_to_stretch = []
    elif isinstance(channel_to_stretch, int):
        channel_to_stretch = [channel_to_stretch]

    # rescale each round independently
    rounds = []
    for r in range(tensor.shape[0]):

        # rescale each channel independently
        channels = []
        for i in range(tensor.shape[1]):
            channel = tensor[r, i, :, :, :]
            if i in channel_to_stretch:
                pa, pb = np.percentile(channel, (0, stretching_percentile))
                channel_rescaled = rescale_intensity(channel,
                                                     in_range=(pa, pb))
            else:
                channel_rescaled = rescale_intensity(channel)
            channels.append(channel_rescaled)
        tensor_4d = np.stack(channels, axis=0)
        rounds.append(tensor_4d)

    tensor_5d = np.stack(rounds, axis=0)

    return tensor_5d


def cast_img_uint8(tensor):
    """Cast the image in np.uint8.

    Casting image to np.uint8 reduce the memory needed to process it and
    accelerate computations.

    Parameters
    ----------
    tensor : np.ndarray
        Image to cast.

    Returns
    -------
    tensor : np.ndarray, np.uint8
        Image cast.

    """
    # TODO validate the warnings
    # check tensor dtype
    check_array(tensor, dtype=[np.uint8, np.uint16,
                               np.float32, np.float64,
                               np.bool])

    if tensor.dtype == np.uint8:
        return tensor

    # check the range value for float tensors
    if tensor.dtype in [np.float32, np.float64]:
        if not check_range_value(tensor, 0, 1):
            raise ValueError("To cast a tensor from {0} to np.uint8, its "
                             "values must be between 0 and 1, and not {1} "
                             "and {2}."
                             .format(tensor.dtype, tensor.min(), tensor.max()))

    # check the range value for integer tensors
    #elif tensor.dtype == np.uint16:
    #    if not check_range_value(tensor, 0, 255):
    #        raise ValueError("To cast a tensor from np.uint16 to np.uint8, "
    #                         "its values must be between 0 and 255, and not "
    #                         "{0} and {1}. Otherwise, the values are clipped."
    #                         .format(tensor.min(), tensor.max()))

    # cast tensor
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tensor = img_as_ubyte(tensor)

    return tensor


def cast_img_uint16(tensor):
    """Cast the data in np.uint16.

    Parameters
    ----------
    tensor : np.ndarray
        Image to cast.

    Returns
    -------
    tensor : np.ndarray, np.uint16
        Image cast.

    """
    # check tensor dtype
    check_array(tensor, dtype=[np.uint8,
                               np.float32, np.float64,
                               np.bool])

    # check the range value for float tensors
    if tensor.dtype in [np.float32, np.float64]:
        if not check_range_value(tensor, 0, 1):
            raise ValueError("To cast a tensor from {0} to np.uint16, its "
                             "values must be between 0 and 1, and not {1} "
                             "and {2}."
                             .format(tensor.dtype, tensor.min(), tensor.max()))

    # cast tensor
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tensor = img_as_uint(tensor)

    return tensor


def cast_img_float32(tensor):
    """Cast the data in np.float32 and scale it between 0 and 1.

    If the input data is already in np.float, the values are not rescaled.

    Casting image to np.float32 reduce the memory needed to process it and
    accelerate computations.

    Parameters
    ----------
    tensor : np.ndarray
        Image to cast.

    Returns
    -------
    tensor : np.ndarray, np.float32
        image cast.

    """
    # check tensor dtype
    check_array(tensor, dtype=[np.uint8, np.uint16,
                               np.float64, np.bool])

    # cast tensor
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tensor = img_as_float32(tensor)

    return tensor


def cast_img_float64(tensor):
    """Cast the data in np.float64 and scale it between 0 and 1.

    If the input data is already in np.float, the values are not rescaled.

    Parameters
    ----------
    tensor : np.ndarray
        Tensor to cast.

    Returns
    -------
    tensor : np.ndarray, np.float64
        Tensor cast.

    """
    # check tensor dtype
    check_array(tensor, dtype=[np.uint8, np.uint16,
                               np.float32,
                               np.bool])

    # cast tensor
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tensor = img_as_float64(tensor)

    return tensor


# ### Filters ###

def _define_kernel(shape, size, dtype):
    """Build a kernel to apply a filter on images.

    Parameters
    ----------
    shape : str
        Shape of the kernel used to compute the filter ('diamond', 'disk',
        'rectangle' or 'square').
    size : int or Tuple(int)
        The size of the kernel. For the rectangle we expect two integers
        (width, height).
    dtype : type
        Dtype used for the kernel (the same as the image).

    Returns
    -------
    kernel : skimage.morphology.selem object
        Kernel to use with a skimage filter.

    """
    # build the kernel
    if shape == "diamond":
        kernel = diamond(size, dtype=dtype)
    elif shape == "disk":
        kernel = disk(size, dtype=dtype)
    elif shape == "rectangle" and isinstance(size, tuple):
        kernel = rectangle(size[0], size[1], dtype=dtype)
    elif shape == "square":
        kernel = square(size, dtype=dtype)
    else:
        raise ValueError("Kernel definition is wrong.")

    return kernel


def mean_filter(image, kernel_shape, kernel_size):
    """Apply a mean filter to a 2-d image.

    Parameters
    ----------
    image : np.ndarray, np.uint
        Image with shape (y, x).
    kernel_shape : str
        Shape of the kernel used to compute the filter ('diamond', 'disk',
        'rectangle' or 'square').
    kernel_size : int or Tuple(int)
        The size of the kernel. For the rectangle we expect two integers
        (width, height).

    Returns
    -------
    image_filtered : np.ndarray, np.uint
        Filtered 2-d image with shape (y, x).

    """
    # check image dtype and ndim
    check_array(image, ndim=2, dtype=[np.uint8, np.uint16])

    # get kernel
    kernel = _define_kernel(shape=kernel_shape,
                            size=kernel_size,
                            dtype=image.dtype)

    # apply filter
    image_filtered = rank.mean(image, kernel)

    return image_filtered


def median_filter(image, kernel_shape, kernel_size):
    """Apply a median filter to a 2-d image.

    Parameters
    ----------
    image : np.ndarray, np.uint
        Image with shape (y, x).
    kernel_shape : str
        Shape of the kernel used to compute the filter ('diamond', 'disk',
        'rectangle' or 'square').
    kernel_size : int or Tuple(int)
        The size of the kernel. For the rectangle we expect two integers
        (width, height).

    Returns
    -------
    image_filtered : np.ndarray, np.uint
        Filtered 2-d image with shape (y, x).

    """
    # check image dtype and ndim
    check_array(image, ndim=2, dtype=[np.uint8, np.uint16])

    # get kernel
    kernel = _define_kernel(shape=kernel_shape,
                            size=kernel_size,
                            dtype=image.dtype)

    # apply filter
    image_filtered = rank.median(image, kernel)

    return image_filtered


def maximum_filter(image, kernel_shape, kernel_size):
    """Apply a maximum filter to a 2-d image.

    Parameters
    ----------
    image : np.ndarray, np.uint
        Image with shape (y, x).
    kernel_shape : str
        Shape of the kernel used to compute the filter ('diamond', 'disk',
        'rectangle' or 'square').
    kernel_size : int or Tuple(int)
        The size of the kernel. For the rectangle we expect two integers
        (width, height).

    Returns
    -------
    image_filtered : np.ndarray, np.uint
        Filtered 2-d image with shape (y, x).

    """
    # check image dtype and ndim
    check_array(image, ndim=2, dtype=[np.uint8, np.uint16])

    # get kernel
    kernel = _define_kernel(shape=kernel_shape,
                            size=kernel_size,
                            dtype=image.dtype)

    # apply filter
    image_filtered = rank.maximum(image, kernel)

    return image_filtered


def minimum_filter(image, kernel_shape, kernel_size):
    """Apply a minimum filter to a 2-d image.

    Parameters
    ----------
    image : np.ndarray, np.uint
        Image with shape (y, x).
    kernel_shape : str
        Shape of the kernel used to compute the filter ('diamond', 'disk',
        'rectangle' or 'square').
    kernel_size : int or Tuple(int)
        The size of the kernel. For the rectangle we expect two integers
        (width, height).

    Returns
    -------
    image_filtered : np.ndarray, np.uint
        Filtered 2-d image with shape (y, x).

    """
    # check image dtype and ndim
    check_array(image, ndim=2, dtype=[np.uint8, np.uint16])

    # get kernel
    kernel = _define_kernel(shape=kernel_shape,
                            size=kernel_size,
                            dtype=image.dtype)

    # apply filter
    image_filtered = rank.minimum(image, kernel)

    return image_filtered


def log_filter(image, sigma):
    """Apply a Laplacian of Gaussian filter to a 2-d or 3-d image.

    The function returns the inverse of the filtered image such that the pixels
    with the highest intensity from the original (smoothed) image have
    positive values. Those with a low intensity returning a negative value are
    clipped to zero.

    Parameters
    ----------
    image : np.ndarray
        Image with shape (z, y, x) or (y, x).
    sigma : float or Tuple(float)
        Sigma used for the gaussian filter (one for each dimension). If it's a
        float, the same sigma is applied to every dimensions.

    Returns
    -------
    image_filtered : np.ndarray, np.float
        Filtered image.
    """
    # check image dtype and ndim
    check_array(image, ndim=[2, 3], dtype=[np.uint8, np.uint16,
                                           np.float32, np.float64])

    # we cast the data in np.float to allow negative values
    image_float = None
    if image.dtype == np.uint8:
        image_float = cast_img_float32(image)
    elif image.dtype == np.uint16:
        image_float = cast_img_float64(image)

    # check sigma
    if isinstance(sigma, (tuple, list)):
        if len(sigma) != image.ndim:
            raise ValueError("'Sigma' must be a scalar or a sequence with the "
                             "same length as 'image.ndim'.")

    # we apply LoG filter
    image_filtered = gaussian_laplace(image_float, sigma=sigma)

    # as the LoG filter makes the peaks in the original image appear as a
    # reversed mexican hat, we inverse the result and clip negative values to 0
    image_filtered = np.clip(-image_filtered, a_min=0, a_max=None)

    return image_filtered


def gaussian_filter(image, sigma):
    """Apply a Gaussian filter to a 2-d or 3-d image.

    Parameters
    ----------
    image : np.ndarray, np.uint
        Image with shape (z, y, x) or (y, x).
    sigma : float or Tuple(float)
        Sigma used for the gaussian filter (one for each dimension). If it's a
        float, the same sigma is applied to every dimensions.

    Returns
    -------
    image_filtered : np.ndarray, np.float
        Filtered image.

    """
    # TODO check for negative values
    # check image dtype and ndim
    check_array(image, ndim=[2, 3], dtype=[np.uint8, np.uint16,
                                           np.float32, np.float64])

    # we cast the data in np.float to allow negative values
    image_float = None
    if image.dtype == np.uint8:
        image_float = cast_img_float32(image)
    elif image.dtype == np.uint16:
        image_float = cast_img_float64(image)

    # we apply gaussian filter
    image_filtered = gaussian(image_float, sigma=sigma)

    return image_filtered


# ### Illumination surface ###

def compute_illumination_surface(stacks, sigma=None):
    """Compute the illumination surface of a specific experiment.

    Parameters
    ----------
    stacks : np.ndarray, np.uint
        Concatenated 5-d tensors along the z-dimension with shape
        (r, c, z, y, x). They represent different images acquired during a
        same experiment.
    sigma : int
        Sigma of the gaussian filtering used to smooth the illumination
        surface.

    Returns
    -------
    illumination_surfaces : np.ndarray, np.float
        A 4-d tensor with shape (r, c, y, x) approximating the average
        differential of illumination in our stack of images, for each channel
        and each round.

    """
    # check stacks dtype and ndim
    check_array(stacks, ndim=5, dtype=[np.uint8, np.uint16])

    # initialize illumination surfaces
    r, c, z, y, x = stacks.shape
    illumination_surfaces = np.zeros((r, c, y, x))

    # compute mean over the z-dimension
    mean_stacks = np.mean(stacks, axis=2)

    # separate the channels and the rounds
    for i_round in range(r):
        for i_channel in range(c):
            illumination_surface = mean_stacks[i_round, i_channel, :, :]

            # smooth the surface
            if sigma is not None:
                illumination_surface = gaussian(illumination_surface, sigma)

            illumination_surfaces[i_round, i_channel] = illumination_surface

    return illumination_surfaces


def correct_illumination_surface(tensor, illumination_surfaces):
    """Correct a tensor with uneven illumination.

    Parameters
    ----------
    tensor : np.ndarray, np.uint
        A 5-d tensor with shape (r, c, z, y, x).
    illumination_surfaces : np.ndarray, np.float
        A 4-d tensor with shape (r, c, y, x) approximating the average
        differential of illumination in our stack of images, for each channel
        and each round.

    Returns
    -------
    tensor_corrected : np.ndarray, np.float
        A 5-d tensor with shape (r, c, z, y, x).

    """
    # check dtype and ndim
    check_array(tensor, ndim=5, dtype=[np.uint8, np.uint16])
    check_array(illumination_surfaces, ndim=4, dtype=[np.float32, np.float64])

    # initialize corrected tensor
    tensor_corrected = np.zeros_like(tensor)

    # TODO control the multiplication and the division
    # correct each round/channel independently
    r, c, _, _, _ = tensor.shape
    for i_round in range(r):
        for i_channel in range(c):
            image_3d = tensor[i_round, i_channel, ...]
            s = illumination_surfaces[i_round, i_channel]
            tensor_corrected[i_round, i_channel] = image_3d * np.mean(s) / s

    return tensor_corrected


# ### Coordinates data cleaning ###

def clean_simulated_data(data, data_cell, path_output=None):
    """Clean simulated dataset.

    Parameters
    ----------
    data : pandas.DataFrame
        Dataframe with all the simulated cells, the coordinates of their
        different elements and the localization pattern used to simulate them.
    data_cell : pandas.DataFrame
        Dataframe with the 2D coordinates of the nucleus and the cytoplasm of
        actual cells used to simulate data.
    path_output : str
        Path to save the cleaned dataset.

    Returns
    -------
    data_final : pandas.DataFrame
        Cleaned dataset.
    background_to_remove : List[str]
        Invalid background.
    id_volume : List[int]
        Background id from 'data_cell' to remove.
    id_rna : List[int]
        Cell id to remove from data.

    """
    # TODO remove the 'SettingWithCopyWarning'
    # filter invalid simulated cell backgrounds
    data_clean, background_to_remove, id_volume = clean_volume(data, data_cell)

    # filter invalid simulated rna spots
    data_clean, id_rna = clean_rna(data_clean)

    # make the feature 'n_rna' consistent
    data_clean["nb_rna"] = data_clean.apply(
        lambda row: len(row["RNA_pos"]),
        axis=1)

    # remove useless features
    data_final = data_clean[
        ['RNA_pos', 'cell_ID', 'pattern_level', 'pattern_name', 'pos_cell',
         'pos_nuc', "nb_rna"]]

    # encode the label
    le = LabelEncoder()
    data_final["label"] = le.fit_transform(data_final["pattern_name"])

    # reset index
    data_final.reset_index(drop=True, inplace=True)

    # save cleaned dataset
    if path_output is not None:
        data_final.to_pickle(path_output)

    return data_final, background_to_remove, id_volume, id_rna


def clean_volume(data, data_cell):
    """Remove misaligned simulated cells from the dataset.

    Parameters
    ----------
    data : pandas.DataFrame
        Dataframe with all the simulated cells, the coordinates of their
        different elements and the localization pattern used to simulate them.
    data_cell : pandas.DataFrame
        Dataframe with the 2D coordinates of the nucleus and the cytoplasm of
        actual cells used to simulate data.

    Returns
    -------
    data_clean : pandas.DataFrame
        Cleaned dataframe.
    background_to_remove : List[str]
        Invalid background.
    id_to_remove : List[int]
        Background id from 'data_cell' to remove.

    """
    # for each cell, check if the volume is valid or not
    data_cell["valid_volume"] = data_cell.apply(
        lambda row: _check_volume(row["pos_cell"], row["pos_nuc"]),
        axis=1)

    # get the invalid backgrounds
    background_to_remove = []
    id_to_remove = []
    for i in data_cell.index:
        if np.logical_not(data_cell.loc[i, "valid_volume"]):
            background_to_remove.append(data_cell.loc[i, "name_img_BGD"])
            id_to_remove.append(i)

    # remove invalid simulated cells
    data_clean = data[~data["name_img_BGD"].isin(background_to_remove)]

    return data_clean, background_to_remove, id_to_remove


def _check_volume(cyto_coord, nuc_coord):
    """Check nucleus coordinates are not outside the boundary of the cytoplasm.

    Parameters
    ----------
    cyto_coord : pandas.Series
        Coordinates of the cytoplasm membrane.
    nuc_coord : pandas.Series
        Coordinates of the nucleus border.

    Returns
    -------
    _ : bool
        Tell if the cell volume is valid or not.

    """
    # get coordinates
    cyto = np.array(cyto_coord)
    nuc = np.array(nuc_coord)

    max_x = max(cyto[:, 0].max() + 5, nuc[:, 0].max() + 5)
    max_y = max(cyto[:, 1].max() + 5, nuc[:, 1].max() + 5)

    # build the dense representation for the cytoplasm
    values = [1] * cyto.shape[0]
    cyto = coo_matrix((values, (cyto[:, 0], cyto[:, 1])),
                      shape=(max_x, max_y)).todense()

    # build the dense representation for the nucleus
    values = [1] * nuc.shape[0]
    nuc = coo_matrix((values, (nuc[:, 0], nuc[:, 1])),
                     shape=(max_x, max_y)).todense()

    # check if the volume is valid
    mask_cyto = ndi.binary_fill_holes(cyto)
    mask_nuc = ndi.binary_fill_holes(nuc)
    frame = np.zeros((max_x, max_y))
    diff = frame - mask_cyto + mask_nuc
    diff = (diff > 0).sum()

    if diff > 0:
        return False
    else:
        return True


def clean_rna(data):
    """Remove cells with misaligned simulated rna spots from the dataset.

    Parameters
    ----------
    data : pandas.DataFrame
        Dataframe with all the simulated cells, the coordinates of their
        different elements and the localization pattern used to simulate them.

    Returns
    -------
    data_clean : pandas.DataFrame
        Cleaned dataframe.
    id_to_remove : List[int]
        Cell id to remove from data.

    """
    # for each cell we check if the rna spots are valid or not
    data["valid_rna"] = data.apply(
        lambda row: _check_rna(row["pos_cell"], row["RNA_pos"]),
        axis=1)

    # get id of the invalid cells
    id_to_remove = []
    for i in data.index:
        if np.logical_not(data.loc[i, "valid_rna"]):
            id_to_remove.append(i)

    # remove invalid simulated cells
    data_clean = data[data["valid_rna"]]

    return data_clean, id_to_remove


def _check_rna(cyto_coord, rna_coord):
    """Check rna spots coordinates are not outside the boundary of the
    cytoplasm.

    Parameters
    ----------
    cyto_coord : pandas.Series
        Coordinates of the cytoplasm membrane.
    rna_coord : pandas.Series
        Coordinates of the rna spots.

    Returns
    -------
    _ : bool
        Tell if the rna spots are valid or not.

    """
    # get coordinates
    cyto = np.array(cyto_coord)
    if not isinstance(rna_coord[0], list):
        # it means we have only one spot
        return False
    rna = np.array(rna_coord)

    # check if the coordinates are positive
    if rna.min() < 0:
        return False

    max_x = int(max(cyto[:, 0].max() + 5, rna[:, 0].max() + 5))
    max_y = int(max(cyto[:, 1].max() + 5, rna[:, 1].max() + 5))

    # build the dense representation for the cytoplasm
    values = [1] * cyto.shape[0]
    cyto = coo_matrix((values, (cyto[:, 0], cyto[:, 1])),
                      shape=(max_x, max_y)).todense()

    # build the dense representation for the rna
    values = [1] * rna.shape[0]
    rna = coo_matrix((values, (rna[:, 0], rna[:, 1])),
                     shape=(max_x, max_y)).todense()
    rna = (rna > 0)

    # check if the coordinates are valid
    mask_cyto = ndi.binary_fill_holes(cyto)
    frame = np.zeros((max_x, max_y))
    diff = frame - mask_cyto + rna
    diff = (diff > 0).sum()

    if diff > 0:
        return False
    else:
        return True
