import configparser
import os
from ia_ui_items import get_sam_model_ids, get_model_ids
import json

ia_config_ini_path = os.path.join(os.path.dirname(__file__), "ia_config.ini")

class IAConfig:
    SECTIONS = ["DEFAULT", "USER"]
    KEYS = ["sam_model_id", "inp_model_id"]

    SECTION_DEFAULT = "DEFAULT"
    SECTION_USER = "USER"

    KEY_SAM_MODEL_ID = "sam_model_id"
    KEY_INP_MODEL_ID = "inp_model_id"
    
    KEY_WEBUI_SAM_MODEL_ID = "inpaint_anything/Segment Anything Model ID/value"
    KEY_WEBUI_INP_MODEL_ID = "inpaint_anything/Inpainting Model ID/value"

def setup_ia_config_ini():
    global ia_config_ini_path
    if not os.path.isfile(ia_config_ini_path):
        ia_config_ini = configparser.ConfigParser()
        
        sam_model_ids = get_sam_model_ids()
        sam_model_index = 1
        inp_model_ids = get_model_ids()
        inp_model_index = 0
        
        ia_config_ini[IAConfig.SECTION_DEFAULT] = {
            IAConfig.KEY_SAM_MODEL_ID: sam_model_ids[sam_model_index],
            IAConfig.KEY_INP_MODEL_ID: inp_model_ids[inp_model_index],
        }
        with open(ia_config_ini_path, "w", encoding="utf-8") as f:
            ia_config_ini.write(f)

def get_ia_config(key, section=None):
    global ia_config_ini_path
    setup_ia_config_ini()
    
    if section is None:
        section = IAConfig.SECTION_DEFAULT
    
    ia_config_ini = configparser.ConfigParser()
    ia_config_ini.read(ia_config_ini_path, encoding="utf-8")
    
    if ia_config_ini.has_option(section, key):
        return ia_config_ini[section][key]

    section = IAConfig.SECTION_DEFAULT
    if ia_config_ini.has_option(section, key):
        return ia_config_ini[section][key]
    
    return None

def get_ia_config_index(key, section=None):
    value = get_ia_config(key, section)
    
    if value is None:
        return None
    
    if key == IAConfig.KEY_SAM_MODEL_ID:
        sam_model_ids = get_sam_model_ids()
        idx = sam_model_ids.index(value) if value in sam_model_ids else 1
        # print("ia_config: get_ia_config_index: key: {}, value: {}, idx: {}".format(key, value, idx))
    elif key == IAConfig.KEY_INP_MODEL_ID:
        inp_model_ids = get_model_ids()
        idx = inp_model_ids.index(value) if value in inp_model_ids else 0
        # print("ia_config: get_ia_config_index: key: {}, value: {}, idx: {}".format(key, value, idx))
    else:
        idx = None

    return idx

def set_ia_config(key, value, section=None):
    global ia_config_ini_path
    setup_ia_config_ini()
    
    if section is None:
        section = IAConfig.SECTION_DEFAULT
    
    ia_config_ini = configparser.ConfigParser()
    ia_config_ini.read(ia_config_ini_path, encoding="utf-8")
    
    if section != IAConfig.SECTION_DEFAULT and not ia_config_ini.has_section(section):
        ia_config_ini[section] = {}
    else:
        if ia_config_ini.has_option(section, key) and ia_config_ini[section][key] == value:
            return
    
    # print("ia_config: set_ia_config: section: {}, key: {}, value: {}".format(section, key, value))
    ia_config_ini[section][key] = value
    
    with open(ia_config_ini_path, "w", encoding="utf-8") as f:
        ia_config_ini.write(f)
