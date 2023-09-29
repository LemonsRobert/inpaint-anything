import argparse
# import math
import gc
import os
import platform

if platform.system() == "Darwin":
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

import random
import traceback
from importlib.util import find_spec

import cv2
import gradio as gr
import numpy as np
import torch
from diffusers import (DDIMScheduler, EulerAncestralDiscreteScheduler, EulerDiscreteScheduler,
                       KDPM2AncestralDiscreteScheduler, KDPM2DiscreteScheduler,
                       StableDiffusionInpaintPipeline)
from lama_cleaner.model_manager import ModelManager
from lama_cleaner.schema import Config, HDStrategy, LDMSampler, SDSampler
from PIL import Image, ImageFilter
from PIL.PngImagePlugin import PngInfo
from torch.hub import download_url_to_file
from torchvision import transforms

import inpalib
from ia_check_versions import ia_check_versions
from ia_config import IAConfig, get_ia_config_index, set_ia_config, setup_ia_config_ini
from ia_devices import devices
from ia_file_manager import IAFileManager, download_model_from_hf, ia_file_manager
from ia_logging import ia_logging
from ia_threading import clear_cache_decorator
from ia_ui_gradio import reload_javascript
from ia_ui_items import (get_cleaner_model_ids, get_inp_model_ids, get_padding_mode_names,
                         get_sam_model_ids, get_sampler_names)

print("platform:", platform.system())

reload_javascript()

if find_spec("xformers") is not None:
    xformers_available = True
else:
    xformers_available = False

parser = argparse.ArgumentParser(description="Inpaint Anything")
parser.add_argument("--save-seg", action="store_true", help="Save the segmentation image generated by SAM.")
parser.add_argument("--offline", action="store_true", help="Execute inpainting using an offline network.")
parser.add_argument("--sam-cpu", action="store_true", help="Perform the Segment Anything operation on CPU.")
args = parser.parse_args()
IAConfig.global_args.update(args.__dict__)


@clear_cache_decorator
def download_model(sam_model_id):
    """Download SAM model.

    Args:
        sam_model_id (str): SAM model id

    Returns:
        str: download status
    """
    if "_hq_" in sam_model_id:
        url_sam = "https://huggingface.co/Uminosachi/sam-hq/resolve/main/" + sam_model_id
    elif "FastSAM" in sam_model_id:
        url_sam = "https://huggingface.co/Uminosachi/FastSAM/resolve/main/" + sam_model_id
    elif "mobile_sam" in sam_model_id:
        url_sam = "https://huggingface.co/Uminosachi/MobileSAM/resolve/main/" + sam_model_id
    else:
        # url_sam_vit_h_4b8939 = "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth"
        url_sam = "https://dl.fbaipublicfiles.com/segment_anything/" + sam_model_id

    sam_checkpoint = os.path.join(ia_file_manager.models_dir, sam_model_id)
    if not os.path.isfile(sam_checkpoint):
        try:
            download_url_to_file(url_sam, sam_checkpoint)
        except Exception as e:
            ia_logging.error(str(e))
            return str(e)

        return IAFileManager.DOWNLOAD_COMPLETE
    else:
        return "Model already exists"


sam_dict = dict(sam_masks=None, mask_image=None, cnet=None, orig_image=None, pad_mask=None)


def save_mask_image(mask_image, save_mask_chk=False):
    """Save mask image.

    Args:
        mask_image (np.ndarray): mask image
        save_mask_chk (bool, optional): If True, save mask image. Defaults to False.

    Returns:
        None
    """
    if save_mask_chk:
        save_name = "_".join([ia_file_manager.savename_prefix, "created_mask"]) + ".png"
        save_name = os.path.join(ia_file_manager.outputs_dir, save_name)
        Image.fromarray(mask_image).save(save_name)


@clear_cache_decorator
def input_image_upload(input_image, sam_image, sel_mask):
    global sam_dict
    sam_dict["orig_image"] = input_image
    sam_dict["pad_mask"] = None

    if (sam_dict["mask_image"] is None or not isinstance(sam_dict["mask_image"], np.ndarray) or
            sam_dict["mask_image"].shape != input_image.shape):
        sam_dict["mask_image"] = np.zeros_like(input_image, dtype=np.uint8)

    ret_sel_image = cv2.addWeighted(input_image, 0.5, sam_dict["mask_image"], 0.5, 0)

    if sam_image is None or not isinstance(sam_image, dict) or "image" not in sam_image:
        sam_dict["sam_masks"] = None
        ret_sam_image = np.zeros_like(input_image, dtype=np.uint8)
    elif sam_image["image"].shape == input_image.shape:
        ret_sam_image = gr.update()
    else:
        sam_dict["sam_masks"] = None
        ret_sam_image = gr.update(value=np.zeros_like(input_image, dtype=np.uint8))

    if sel_mask is None or not isinstance(sel_mask, dict) or "image" not in sel_mask:
        ret_sel_mask = ret_sel_image
    elif sel_mask["image"].shape == ret_sel_image.shape and np.all(sel_mask["image"] == ret_sel_image):
        ret_sel_mask = gr.update()
    else:
        ret_sel_mask = gr.update(value=ret_sel_image)

    return ret_sam_image, ret_sel_mask, gr.update(interactive=True)


@clear_cache_decorator
def run_padding(input_image, pad_scale_width, pad_scale_height, pad_lr_barance, pad_tb_barance, padding_mode="edge"):
    global sam_dict
    if input_image is None or sam_dict["orig_image"] is None:
        sam_dict["orig_image"] = None
        sam_dict["pad_mask"] = None
        return None, "Input image not found"

    orig_image = sam_dict["orig_image"]

    height, width = orig_image.shape[:2]
    pad_width, pad_height = (int(width * pad_scale_width), int(height * pad_scale_height))
    ia_logging.info(f"resize by padding: ({height}, {width}) -> ({pad_height}, {pad_width})")

    pad_size_w, pad_size_h = (pad_width - width, pad_height - height)
    pad_size_l = int(pad_size_w * pad_lr_barance)
    pad_size_r = pad_size_w - pad_size_l
    pad_size_t = int(pad_size_h * pad_tb_barance)
    pad_size_b = pad_size_h - pad_size_t

    pad_width = [(pad_size_t, pad_size_b), (pad_size_l, pad_size_r), (0, 0)]
    if padding_mode == "constant":
        fill_value = 127
        pad_image = np.pad(orig_image, pad_width=pad_width, mode=padding_mode, constant_values=fill_value)
    else:
        pad_image = np.pad(orig_image, pad_width=pad_width, mode=padding_mode)

    mask_pad_width = [(pad_size_t, pad_size_b), (pad_size_l, pad_size_r)]
    pad_mask = np.zeros((height, width), dtype=np.uint8)
    pad_mask = np.pad(pad_mask, pad_width=mask_pad_width, mode="constant", constant_values=255)
    sam_dict["pad_mask"] = dict(segmentation=pad_mask.astype(bool))

    return pad_image, "Padding done"


@clear_cache_decorator
def run_sam(input_image, sam_model_id, sam_image, anime_style_chk=False):
    global sam_dict
    if not inpalib.sam_file_exists(sam_model_id):
        ret_sam_image = None if sam_image is None else gr.update()
        return ret_sam_image, f"{sam_model_id} not found, please download"

    if input_image is None:
        ret_sam_image = None if sam_image is None else gr.update()
        return ret_sam_image, "Input image not found"

    set_ia_config(IAConfig.KEYS.SAM_MODEL_ID, sam_model_id, IAConfig.SECTIONS.USER)

    if sam_dict["sam_masks"] is not None:
        sam_dict["sam_masks"] = None
        gc.collect()

    ia_logging.info(f"input_image: {input_image.shape} {input_image.dtype}")

    try:
        sam_masks = inpalib.generate_sam_masks(input_image, sam_model_id, anime_style_chk)
        sam_masks = inpalib.sort_masks_by_area(sam_masks)
        sam_masks = inpalib.insert_mask_to_sam_masks(sam_masks, sam_dict["pad_mask"])

        seg_image = inpalib.create_seg_color_image(input_image, sam_masks)

        sam_dict["sam_masks"] = sam_masks

    except Exception as e:
        print(traceback.format_exc())
        ia_logging.error(str(e))
        ret_sam_image = None if sam_image is None else gr.update()
        return ret_sam_image, "Segment Anything failed"

    if IAConfig.global_args.get("save_seg", False):
        save_name = "_".join([ia_file_manager.savename_prefix, os.path.splitext(sam_model_id)[0]]) + ".png"
        save_name = os.path.join(ia_file_manager.outputs_dir, save_name)
        Image.fromarray(seg_image).save(save_name)

    if sam_image is None:
        return seg_image, "Segment Anything complete"
    else:
        if sam_image["image"].shape == seg_image.shape and np.all(sam_image["image"] == seg_image):
            return gr.update(), "Segment Anything complete"
        else:
            return gr.update(value=seg_image), "Segment Anything complete"


@clear_cache_decorator
def select_mask(input_image, sam_image, invert_chk, ignore_black_chk, sel_mask):
    global sam_dict
    if sam_dict["sam_masks"] is None or sam_image is None:
        ret_sel_mask = None if sel_mask is None else gr.update()
        return ret_sel_mask
    sam_masks = sam_dict["sam_masks"]

    # image = sam_image["image"]
    mask = sam_image["mask"][:, :, 0:1]

    try:
        seg_image = inpalib.create_mask_image(mask, sam_masks, ignore_black_chk)
        if invert_chk:
            seg_image = inpalib.invert_mask(seg_image)

        sam_dict["mask_image"] = seg_image

    except Exception as e:
        print(traceback.format_exc())
        ia_logging.error(str(e))
        ret_sel_mask = None if sel_mask is None else gr.update()
        return ret_sel_mask

    if input_image is not None and input_image.shape == seg_image.shape:
        ret_image = cv2.addWeighted(input_image, 0.5, seg_image, 0.5, 0)
    else:
        ret_image = seg_image

    if sel_mask is None:
        return ret_image
    else:
        if sel_mask["image"].shape == ret_image.shape and np.all(sel_mask["image"] == ret_image):
            return gr.update()
        else:
            return gr.update(value=ret_image)


@clear_cache_decorator
def expand_mask(input_image, sel_mask, expand_iteration=1):
    global sam_dict
    if sam_dict["mask_image"] is None or sel_mask is None:
        return None

    new_sel_mask = sam_dict["mask_image"]

    expand_iteration = int(np.clip(expand_iteration, 1, 100))

    new_sel_mask = cv2.dilate(new_sel_mask, np.ones((3, 3), dtype=np.uint8), iterations=expand_iteration)

    sam_dict["mask_image"] = new_sel_mask

    if input_image is not None and input_image.shape == new_sel_mask.shape:
        ret_image = cv2.addWeighted(input_image, 0.5, new_sel_mask, 0.5, 0)
    else:
        ret_image = new_sel_mask

    if sel_mask["image"].shape == ret_image.shape and np.all(sel_mask["image"] == ret_image):
        return gr.update()
    else:
        return gr.update(value=ret_image)


@clear_cache_decorator
def apply_mask(input_image, sel_mask):
    global sam_dict
    if sam_dict["mask_image"] is None or sel_mask is None:
        return None

    sel_mask_image = sam_dict["mask_image"]
    sel_mask_mask = np.logical_not(sel_mask["mask"][:, :, 0:3].astype(bool)).astype(np.uint8)
    new_sel_mask = sel_mask_image * sel_mask_mask

    sam_dict["mask_image"] = new_sel_mask

    if input_image is not None and input_image.shape == new_sel_mask.shape:
        ret_image = cv2.addWeighted(input_image, 0.5, new_sel_mask, 0.5, 0)
    else:
        ret_image = new_sel_mask

    if sel_mask["image"].shape == ret_image.shape and np.all(sel_mask["image"] == ret_image):
        return gr.update()
    else:
        return gr.update(value=ret_image)


@clear_cache_decorator
def add_mask(input_image, sel_mask):
    global sam_dict
    if sam_dict["mask_image"] is None or sel_mask is None:
        return None

    sel_mask_image = sam_dict["mask_image"]
    sel_mask_mask = sel_mask["mask"][:, :, 0:3].astype(bool).astype(np.uint8)
    new_sel_mask = sel_mask_image + (sel_mask_mask * np.invert(sel_mask_image, dtype=np.uint8))

    sam_dict["mask_image"] = new_sel_mask

    if input_image is not None and input_image.shape == new_sel_mask.shape:
        ret_image = cv2.addWeighted(input_image, 0.5, new_sel_mask, 0.5, 0)
    else:
        ret_image = new_sel_mask

    if sel_mask["image"].shape == ret_image.shape and np.all(sel_mask["image"] == ret_image):
        return gr.update()
    else:
        return gr.update(value=ret_image)


def auto_resize_to_pil(input_image, mask_image):
    init_image = Image.fromarray(input_image).convert("RGB")
    mask_image = Image.fromarray(mask_image).convert("RGB")
    assert init_image.size == mask_image.size, "The sizes of the image and mask do not match"
    width, height = init_image.size

    new_height = (height // 8) * 8
    new_width = (width // 8) * 8
    if new_width < width or new_height < height:
        if (new_width / width) < (new_height / height):
            scale = new_height / height
        else:
            scale = new_width / width
        resize_height = int(height*scale+0.5)
        resize_width = int(width*scale+0.5)
        if height != resize_height or width != resize_width:
            ia_logging.info(f"resize: ({height}, {width}) -> ({resize_height}, {resize_width})")
            init_image = transforms.functional.resize(init_image, (resize_height, resize_width), transforms.InterpolationMode.LANCZOS)
            mask_image = transforms.functional.resize(mask_image, (resize_height, resize_width), transforms.InterpolationMode.LANCZOS)
        if resize_height != new_height or resize_width != new_width:
            ia_logging.info(f"center_crop: ({resize_height}, {resize_width}) -> ({new_height}, {new_width})")
            init_image = transforms.functional.center_crop(init_image, (new_height, new_width))
            mask_image = transforms.functional.center_crop(mask_image, (new_height, new_width))

    return init_image, mask_image


@clear_cache_decorator
def run_inpaint(input_image, sel_mask, prompt, n_prompt, ddim_steps, cfg_scale, seed, inp_model_id, save_mask_chk, composite_chk,
                sampler_name="DDIM", iteration_count=1):
    global sam_dict
    if input_image is None or sam_dict["mask_image"] is None or sel_mask is None:
        ia_logging.error("The image or mask does not exist")
        return

    mask_image = sam_dict["mask_image"]
    if input_image.shape != mask_image.shape:
        ia_logging.error("The sizes of the image and mask do not match")
        return

    set_ia_config(IAConfig.KEYS.INP_MODEL_ID, inp_model_id, IAConfig.SECTIONS.USER)

    save_mask_image(mask_image, save_mask_chk)

    ia_logging.info(f"Loading model {inp_model_id}")
    config_offline_inpainting = IAConfig.global_args.get("offline", False)
    if config_offline_inpainting:
        ia_logging.info("Run Inpainting on offline network: {}".format(str(config_offline_inpainting)))
    local_files_only = False
    local_file_status = download_model_from_hf(inp_model_id, local_files_only=True)
    if local_file_status != IAFileManager.DOWNLOAD_COMPLETE:
        if config_offline_inpainting:
            ia_logging.warning(local_file_status)
            return
    else:
        local_files_only = True
        ia_logging.info("local_files_only: {}".format(str(local_files_only)))

    if platform.system() == "Darwin" or devices.device == devices.cpu or ia_check_versions.torch_on_amd_rocm:
        torch_dtype = torch.float32
    else:
        torch_dtype = torch.float16

    try:
        pipe = StableDiffusionInpaintPipeline.from_pretrained(inp_model_id, torch_dtype=torch_dtype, local_files_only=local_files_only)
    except Exception as e:
        ia_logging.error(str(e))
        if not config_offline_inpainting:
            try:
                pipe = StableDiffusionInpaintPipeline.from_pretrained(inp_model_id, torch_dtype=torch_dtype, resume_download=True)
            except Exception as e:
                ia_logging.error(str(e))
                try:
                    pipe = StableDiffusionInpaintPipeline.from_pretrained(inp_model_id, torch_dtype=torch_dtype, force_download=True)
                except Exception as e:
                    ia_logging.error(str(e))
                    return
        else:
            return
    pipe.safety_checker = None

    ia_logging.info(f"Using sampler {sampler_name}")
    if sampler_name == "DDIM":
        pipe.scheduler = DDIMScheduler.from_config(pipe.scheduler.config)
    elif sampler_name == "Euler":
        pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config)
    elif sampler_name == "Euler a":
        pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
    elif sampler_name == "DPM2 Karras":
        pipe.scheduler = KDPM2DiscreteScheduler.from_config(pipe.scheduler.config)
    elif sampler_name == "DPM2 a Karras":
        pipe.scheduler = KDPM2AncestralDiscreteScheduler.from_config(pipe.scheduler.config)
    else:
        ia_logging.info("Sampler fallback to DDIM")
        pipe.scheduler = DDIMScheduler.from_config(pipe.scheduler.config)

    if platform.system() == "Darwin":
        pipe = pipe.to("mps" if ia_check_versions.torch_mps_is_available else "cpu")
        pipe.enable_attention_slicing()
        torch_generator = torch.Generator(devices.cpu)
    else:
        if ia_check_versions.diffusers_enable_cpu_offload and devices.device != devices.cpu:
            ia_logging.info("Enable model cpu offload")
            pipe.enable_model_cpu_offload()
        else:
            pipe = pipe.to(devices.device)
        if xformers_available:
            ia_logging.info("Enable xformers memory efficient attention")
            pipe.enable_xformers_memory_efficient_attention()
        else:
            ia_logging.info("Enable attention slicing")
            pipe.enable_attention_slicing()
        if "privateuseone" in str(getattr(devices.device, "type", "")):
            torch_generator = torch.Generator(devices.cpu)
        else:
            torch_generator = torch.Generator(devices.device)

    init_image, mask_image = auto_resize_to_pil(input_image, mask_image)
    width, height = init_image.size

    output_list = []
    for count in range(int(iteration_count)):
        gc.collect()
        if seed < 0 or count > 0:
            seed = random.randint(0, 2147483647)

        generator = torch_generator.manual_seed(seed)

        pipe_args_dict = {
            "prompt": prompt,
            "image": init_image,
            "width": width,
            "height": height,
            "mask_image": mask_image,
            "num_inference_steps": ddim_steps,
            "guidance_scale": cfg_scale,
            "negative_prompt": n_prompt,
            "generator": generator,
        }

        output_image = pipe(**pipe_args_dict).images[0]

        if composite_chk:
            dilate_mask_image = Image.fromarray(cv2.dilate(np.array(mask_image), np.ones((3, 3), dtype=np.uint8), iterations=4))
            output_image = Image.composite(output_image, init_image, dilate_mask_image.convert("L").filter(ImageFilter.GaussianBlur(3)))

        generation_params = {
            "Steps": ddim_steps,
            "Sampler": sampler_name,
            "CFG scale": cfg_scale,
            "Seed": seed,
            "Size": f"{width}x{height}",
            "Model": inp_model_id,
        }

        generation_params_text = ", ".join([k if k == v else f"{k}: {v}" for k, v in generation_params.items() if v is not None])
        prompt_text = prompt if prompt else ""
        negative_prompt_text = "\nNegative prompt: " + n_prompt if n_prompt else ""
        infotext = f"{prompt_text}{negative_prompt_text}\n{generation_params_text}".strip()

        metadata = PngInfo()
        metadata.add_text("parameters", infotext)

        save_name = "_".join([ia_file_manager.savename_prefix, os.path.basename(inp_model_id), str(seed)]) + ".png"
        save_name = os.path.join(ia_file_manager.outputs_dir, save_name)
        output_image.save(save_name, pnginfo=metadata)

        output_list.append(output_image)

        yield output_list, max([1, iteration_count - (count + 1)])


@clear_cache_decorator
def run_cleaner(input_image, sel_mask, cleaner_model_id, cleaner_save_mask_chk):
    global sam_dict
    if input_image is None or sam_dict["mask_image"] is None or sel_mask is None:
        ia_logging.error("The image or mask does not exist")
        return None

    mask_image = sam_dict["mask_image"]
    if input_image.shape != mask_image.shape:
        ia_logging.error("The sizes of the image and mask do not match")
        return None

    save_mask_image(mask_image, cleaner_save_mask_chk)

    ia_logging.info(f"Loading model {cleaner_model_id}")
    if platform.system() == "Darwin":
        model = ModelManager(name=cleaner_model_id, device=devices.cpu)
    else:
        model = ModelManager(name=cleaner_model_id, device=devices.device)

    init_image, mask_image = auto_resize_to_pil(input_image, mask_image)
    width, height = init_image.size

    init_image = np.array(init_image)
    mask_image = np.array(mask_image.convert("L"))

    config = Config(
        ldm_steps=20,
        ldm_sampler=LDMSampler.ddim,
        hd_strategy=HDStrategy.ORIGINAL,
        hd_strategy_crop_margin=32,
        hd_strategy_crop_trigger_size=512,
        hd_strategy_resize_limit=512,
        prompt="",
        sd_steps=20,
        sd_sampler=SDSampler.ddim
    )

    output_image = model(image=init_image, mask=mask_image, config=config)
    output_image = cv2.cvtColor(output_image.astype(np.uint8), cv2.COLOR_BGR2RGB)
    output_image = Image.fromarray(output_image)

    save_name = "_".join([ia_file_manager.savename_prefix, os.path.basename(cleaner_model_id)]) + ".png"
    save_name = os.path.join(ia_file_manager.outputs_dir, save_name)
    output_image.save(save_name)

    del model
    return [output_image]


@clear_cache_decorator
def run_get_alpha_image(input_image, sel_mask):
    global sam_dict
    if input_image is None or sam_dict["mask_image"] is None or sel_mask is None:
        ia_logging.error("The image or mask does not exist")
        return None, ""

    mask_image = sam_dict["mask_image"]
    if input_image.shape != mask_image.shape:
        ia_logging.error("The sizes of the image and mask do not match")
        return None, ""

    alpha_image = Image.fromarray(input_image).convert("RGBA")
    mask_image = Image.fromarray(mask_image).convert("L")

    alpha_image.putalpha(mask_image)

    save_name = "_".join([ia_file_manager.savename_prefix, "rgba_image"]) + ".png"
    save_name = os.path.join(ia_file_manager.outputs_dir, save_name)
    alpha_image.save(save_name)

    return alpha_image, f"saved: {save_name}"


@clear_cache_decorator
def run_get_mask(sel_mask):
    global sam_dict
    if sam_dict["mask_image"] is None or sel_mask is None:
        return None

    mask_image = sam_dict["mask_image"]

    save_name = "_".join([ia_file_manager.savename_prefix, "created_mask"]) + ".png"
    save_name = os.path.join(ia_file_manager.outputs_dir, save_name)
    Image.fromarray(mask_image).save(save_name)

    return mask_image


def on_ui_tabs():
    setup_ia_config_ini()
    sampler_names = get_sampler_names()
    sam_model_ids = get_sam_model_ids()
    sam_model_index = get_ia_config_index(IAConfig.KEYS.SAM_MODEL_ID, IAConfig.SECTIONS.USER)
    inp_model_ids = get_inp_model_ids()
    inp_model_index = get_ia_config_index(IAConfig.KEYS.INP_MODEL_ID, IAConfig.SECTIONS.USER)
    cleaner_model_ids = get_cleaner_model_ids()
    padding_mode_names = get_padding_mode_names()

    block = gr.Blocks().queue()
    block.title = "Inpaint Anything"
    with block as inpaint_anything_interface:
        with gr.Row():
            gr.Markdown("## Inpainting with Segment Anything")
        with gr.Row():
            with gr.Column():
                with gr.Row():
                    with gr.Column():
                        sam_model_id = gr.Dropdown(label="Segment Anything Model ID", elem_id="sam_model_id", choices=sam_model_ids,
                                                   value=sam_model_ids[sam_model_index], show_label=True)
                    with gr.Column():
                        with gr.Row():
                            load_model_btn = gr.Button("Download model", elem_id="load_model_btn")
                        with gr.Row():
                            status_text = gr.Textbox(label="", elem_id="status_text", max_lines=1, show_label=False, interactive=False)
                with gr.Row():
                    input_image = gr.Image(label="Input image", elem_id="ia_input_image", source="upload", type="numpy", interactive=True)

                with gr.Row():
                    with gr.Accordion("Padding options", elem_id="padding_options", open=False):
                        with gr.Row():
                            with gr.Column():
                                pad_scale_width = gr.Slider(label="Scale Width", elem_id="pad_scale_width", minimum=1.0, maximum=1.5, value=1.0, step=0.01)
                            with gr.Column():
                                pad_lr_barance = gr.Slider(label="Left/Right Balance", elem_id="pad_lr_barance", minimum=0.0, maximum=1.0, value=0.5, step=0.01)
                        with gr.Row():
                            with gr.Column():
                                pad_scale_height = gr.Slider(label="Scale Height", elem_id="pad_scale_height", minimum=1.0, maximum=1.5, value=1.0, step=0.01)
                            with gr.Column():
                                pad_tb_barance = gr.Slider(label="Top/Bottom Balance", elem_id="pad_tb_barance", minimum=0.0, maximum=1.0, value=0.5, step=0.01)
                        with gr.Row():
                            with gr.Column():
                                padding_mode = gr.Dropdown(label="Padding Mode", elem_id="padding_mode", choices=padding_mode_names, value="edge")
                            with gr.Column():
                                padding_btn = gr.Button("Run Padding", elem_id="padding_btn")

                with gr.Row():
                    with gr.Column():
                        anime_style_chk = gr.Checkbox(label="Anime Style (Up Detection, Down mask Quality)", elem_id="anime_style_chk",
                                                      show_label=True, interactive=True)
                    with gr.Column():
                        sam_btn = gr.Button("Run Segment Anything", elem_id="sam_btn", variant="primary", interactive=False)

                with gr.Tab("Inpainting", elem_id="inpainting_tab"):
                    prompt = gr.Textbox(label="Inpainting Prompt", elem_id="sd_prompt")
                    n_prompt = gr.Textbox(label="Negative Prompt", elem_id="sd_n_prompt")
                    with gr.Accordion("Advanced options", elem_id="inp_advanced_options", open=False):
                        composite_chk = gr.Checkbox(label="Mask area Only", elem_id="composite_chk", value=True, show_label=True, interactive=True)
                        with gr.Row():
                            with gr.Column():
                                sampler_name = gr.Dropdown(label="Sampler", elem_id="sampler_name", choices=sampler_names,
                                                           value=sampler_names[0], show_label=True)
                            with gr.Column():
                                ddim_steps = gr.Slider(label="Sampling Steps", elem_id="ddim_steps", minimum=1, maximum=100, value=20, step=1)
                        cfg_scale = gr.Slider(label="Guidance Scale", elem_id="cfg_scale", minimum=0.1, maximum=30.0, value=7.5, step=0.1)
                        seed = gr.Slider(
                            label="Seed",
                            elem_id="sd_seed",
                            minimum=-1,
                            maximum=2147483647,
                            step=1,
                            value=-1,
                        )
                    with gr.Row():
                        with gr.Column():
                            inp_model_id = gr.Dropdown(label="Inpainting Model ID", elem_id="inp_model_id",
                                                       choices=inp_model_ids, value=inp_model_ids[inp_model_index], show_label=True)
                        with gr.Column():
                            with gr.Row():
                                inpaint_btn = gr.Button("Run Inpainting", elem_id="inpaint_btn", variant="primary")
                            with gr.Row():
                                save_mask_chk = gr.Checkbox(label="Save mask", elem_id="save_mask_chk",
                                                            value=False, show_label=False, interactive=False, visible=False)
                                iteration_count = gr.Slider(label="Iterations", elem_id="iteration_count", minimum=1, maximum=10, value=1, step=1)

                    with gr.Row():
                        out_image = gr.Gallery(label="Inpainted image", elem_id="ia_out_image", show_label=False, columns=2, height=512)

                with gr.Tab("Cleaner", elem_id="cleaner_tab"):
                    with gr.Row():
                        with gr.Column():
                            cleaner_model_id = gr.Dropdown(label="Cleaner Model ID", elem_id="cleaner_model_id",
                                                           choices=cleaner_model_ids, value=cleaner_model_ids[0], show_label=True)
                        with gr.Column():
                            with gr.Row():
                                cleaner_btn = gr.Button("Run Cleaner", elem_id="cleaner_btn", variant="primary")
                            with gr.Row():
                                cleaner_save_mask_chk = gr.Checkbox(label="Save mask", elem_id="cleaner_save_mask_chk",
                                                                    value=False, show_label=False, interactive=False, visible=False)

                    with gr.Row():
                        cleaner_out_image = gr.Gallery(label="Cleaned image", elem_id="ia_cleaner_out_image", show_label=False, columns=2, height=512)

                with gr.Tab("Mask only", elem_id="mask_only_tab"):
                    with gr.Row():
                        with gr.Column():
                            get_alpha_image_btn = gr.Button("Get mask as alpha of image", elem_id="get_alpha_image_btn")
                        with gr.Column():
                            get_mask_btn = gr.Button("Get mask", elem_id="get_mask_btn")

                    with gr.Row():
                        with gr.Column():
                            alpha_out_image = gr.Image(label="Alpha channel image", elem_id="alpha_out_image", type="pil", image_mode="RGBA", interactive=False)
                        with gr.Column():
                            mask_out_image = gr.Image(label="Mask image", elem_id="mask_out_image", type="numpy", interactive=False)

                    with gr.Row():
                        with gr.Column():
                            get_alpha_status_text = gr.Textbox(label="", elem_id="get_alpha_status_text", max_lines=1, show_label=False, interactive=False)
                        with gr.Column():
                            gr.Markdown("")

            with gr.Column():
                with gr.Row():
                    gr.Markdown("Mouse over image: Press `S` key for Fullscreen mode, `R` key to Reset zoom")
                with gr.Row():
                    sam_image = gr.Image(label="Segment Anything image", elem_id="ia_sam_image", type="numpy", tool="sketch", brush_radius=8,
                                         show_label=False, interactive=True).style(height=480)
                with gr.Row():
                    with gr.Column():
                        select_btn = gr.Button("Create Mask", elem_id="select_btn", variant="primary")
                    with gr.Column():
                        with gr.Row():
                            invert_chk = gr.Checkbox(label="Invert mask", elem_id="invert_chk", show_label=True, interactive=True)
                            ignore_black_chk = gr.Checkbox(label="Ignore black area", elem_id="ignore_black_chk", value=True, show_label=True, interactive=True)

                with gr.Row():
                    sel_mask = gr.Image(label="Selected mask image", elem_id="ia_sel_mask", type="numpy", tool="sketch", brush_radius=12,
                                        show_label=False, interactive=True).style(height=480)

                with gr.Row().style(equal_height=False):
                    with gr.Column():
                        expand_mask_btn = gr.Button("Expand mask region", elem_id="expand_mask_btn")
                        expand_mask_iteration_count = gr.Slider(label="Expand Mask Iterations",
                                                                elem_id="expand_mask_iteration_count", minimum=1, maximum=100, value=1, step=1)
                    with gr.Column():
                        apply_mask_btn = gr.Button("Trim mask by sketch", elem_id="apply_mask_btn")
                        add_mask_btn = gr.Button("Add mask by sketch", elem_id="add_mask_btn")

            load_model_btn.click(download_model, inputs=[sam_model_id], outputs=[status_text])
            input_image.upload(input_image_upload, inputs=[input_image, sam_image, sel_mask], outputs=[sam_image, sel_mask, sam_btn]).then(
                fn=None, inputs=None, outputs=None, _js="inpaintAnything_initSamSelMask")
            padding_btn.click(run_padding, inputs=[input_image, pad_scale_width, pad_scale_height, pad_lr_barance, pad_tb_barance, padding_mode],
                              outputs=[input_image, status_text])
            sam_btn.click(run_sam, inputs=[input_image, sam_model_id, sam_image, anime_style_chk], outputs=[sam_image, status_text]).then(
                fn=None, inputs=None, outputs=None, _js="inpaintAnything_clearSamMask")
            select_btn.click(select_mask, inputs=[input_image, sam_image, invert_chk, ignore_black_chk, sel_mask], outputs=[sel_mask]).then(
                fn=None, inputs=None, outputs=None, _js="inpaintAnything_clearSelMask")
            expand_mask_btn.click(expand_mask, inputs=[input_image, sel_mask, expand_mask_iteration_count], outputs=[sel_mask]).then(
                fn=None, inputs=None, outputs=None, _js="inpaintAnything_clearSelMask")
            apply_mask_btn.click(apply_mask, inputs=[input_image, sel_mask], outputs=[sel_mask]).then(
                fn=None, inputs=None, outputs=None, _js="inpaintAnything_clearSelMask")
            add_mask_btn.click(add_mask, inputs=[input_image, sel_mask], outputs=[sel_mask]).then(
                fn=None, inputs=None, outputs=None, _js="inpaintAnything_clearSelMask")

            inpaint_btn.click(
                run_inpaint,
                inputs=[input_image, sel_mask, prompt, n_prompt, ddim_steps, cfg_scale, seed, inp_model_id, save_mask_chk, composite_chk,
                        sampler_name, iteration_count],
                outputs=[out_image, iteration_count])
            cleaner_btn.click(
                run_cleaner,
                inputs=[input_image, sel_mask, cleaner_model_id, cleaner_save_mask_chk],
                outputs=[cleaner_out_image])
            get_alpha_image_btn.click(
                run_get_alpha_image,
                inputs=[input_image, sel_mask],
                outputs=[alpha_out_image, get_alpha_status_text])
            get_mask_btn.click(
                run_get_mask,
                inputs=[sel_mask],
                outputs=[mask_out_image])

    return [(inpaint_anything_interface, "Inpaint Anything", "inpaint_anything")]


block, _, _ = on_ui_tabs()[0]
block.launch()
