from argparse import ArgumentParser
import h5py
import json
import copy
import importlib
import numpy as np
from enum import Enum
import random
from typing import List, Dict, Tuple
from weighted_collection import WeightedCollection
from tdw.tdw_utils import TDWUtils
from tdw.librarian import ModelRecord, MaterialLibrarian
from tdw.output_data import OutputData, Transforms
from tdw_physics.rigidbodies_dataset import (RigidbodiesDataset,
                                             get_random_xyz_transform,
                                             get_range,
                                             handle_random_transform_args)
from tdw_physics.util import MODEL_LIBRARIES, get_parser, xyz_to_arr, arr_to_xyz, str_to_xyz

from tdw_physics.target_controllers.dominoes import Dominoes, MultiDominoes, get_args, none_or_str, none_or_int
from tdw_physics.target_controllers.collision import Collision
from tdw_physics.postprocessing.labels import is_trial_valid

MODEL_NAMES = [r.name for r in MODEL_LIBRARIES['models_full.json'].records]
PRIMITIVE_NAMES = [r.name for r in MODEL_LIBRARIES['models_flex.json'].records]
M = MaterialLibrarian()
MATERIAL_TYPES = M.get_material_types()
MATERIAL_NAMES = {mtype: [m.name for m in M.get_all_materials_of_type(mtype)] \
                  for mtype in MATERIAL_TYPES}

ALL_CATEGORIES = list(set([r.wcategory for r in MODEL_LIBRARIES['models_full.json'].records]))

def get_playroom_args(dataset_dir: str, parse=True):

    common = get_parser(dataset_dir, get_help=False)
    domino, domino_postproc = get_args(dataset_dir, parse=False)
    parser = ArgumentParser(parents=[common, domino], conflict_handler='resolve', fromfile_prefix_chars='@')

    ## Changed defaults
    ### zone
    parser.add_argument("--zscale",
                        type=str,
                        default="[2.0,4.0],0.01,[2.0,4.0]",
                        help="scale of target zone")
    parser.add_argument("--zmaterial",
                        type=none_or_str,
                        default=None,
                        help="material of zone")    

    parser.add_argument("--zone",
                        type=str,
                        default="cube",
                        help="comma-separated list of possible target zone shapes")

    parser.add_argument("--zjitter",
                        type=float,
                        default=0.35,
                        help="amount of z jitter applied to the target zone")

    ### probe
    parser.add_argument("--plift",
                        type=float,
                        default=0.,
                        help="Lift the probe object off the floor. Useful for rotated objects")

    ### force
    parser.add_argument("--fscale",
                        type=str,
                        default="[5.0,10.0]",
                        help="range of scales to apply to push force")
    parser.add_argument("--fwait",
                        type=none_or_str,
                        default="10",
                        help="range of time steps to apply to wait to apply force")    

    parser.add_argument("--frot",
                        type=str,
                        default="[-20,20]",
                        help="range of angles in xz plane to apply push force")

    parser.add_argument("--foffset",
                        type=str,
                        default="0.0,0.8,0.0",
                        help="offset from probe centroid from which to apply force, relative to probe scale")

    parser.add_argument("--fjitter",
                        type=float,
                        default=0.5,
                        help="jitter around object centroid to apply force")

    
    ###target
    parser.add_argument("--target",
                        type=none_or_str,
                        default=','.join(MODEL_NAMES),
                        help="comma-separated list of possible target objects")
    parser.add_argument("--target_categories",
                        type=none_or_str,
                        default=None,
                        help="Allowable target categories")    

    parser.add_argument("--tscale",
                        type=str,
                        default="[0.5,1.0]",
                        help="scale of target objects")

    ### probe
    parser.add_argument("--probe",
                        type=none_or_str,
                        default=','.join(MODEL_NAMES),
                        help="comma-separated list of possible target objects")
    parser.add_argument("--probe_categories",
                        type=none_or_str,
                        default=None,
                        help="Allowable probe categories")

    parser.add_argument("--pscale",
                        type=str,
                        default="[0.5,1.0]",
                        help="scale of probe objects")    
    

    ### layout
    parser.add_argument("--collision_axis_length",
                        type=float,
                        default=2.0,
                        help="Length of spacing between probe and target objects at initialization.")
    
    ## collision specific arguments
    parser.add_argument("--fupforce",
                        type=none_or_str,
                        default="[0.1,0.75]",
                        help="Upwards component of force applied, with 0 being purely horizontal force and 1 being the same force being applied horizontally applied vertically.")

    ## camera
    parser.add_argument("--camera_min_angle",
                        type=float,
                        default=0,
                        help="minimum angle of camera rotation around centerpoint")
    parser.add_argument("--camera_max_angle",
                        type=float,
                        default=360,
                        help="maximum angle of camera rotation around centerpoint")
    parser.add_argument("--camera_distance",
                        type=none_or_str,
                        default="[1.5,3.0]",
                        help="radial distance from camera to centerpoint")

    ## occluders and distractors
    parser.add_argument("--occluder_aspect_ratio",
                        type=none_or_str,
                        default="[0.5,2.5]",
                        help="The range of valid occluder aspect ratios")
    parser.add_argument("--distractor_aspect_ratio",
                        type=none_or_str,
                        default="[0.25,5.0]",
                        help="The range of valid distractor aspect ratios")       
    parser.add_argument("--occluder_categories",
                        type=none_or_str,
                        default=ALL_CATEGORIES,
                        help="the category ids to sample occluders from")
    parser.add_argument("--distractor_categories",
                        type=none_or_str,
                        default=ALL_CATEGORIES,
                        help="the category ids to sample distractors from")
    parser.add_argument("--num_occluders",
                        type=none_or_int,
                        default=1,
                        help="number of occluders")
    parser.add_argument("--num_distractors",
                        type=none_or_int,
                        default=3,
                        help="number of distractors")    
 
    def postprocess(args):
        args.fupforce = handle_random_transform_args(args.fupforce)

        ## don't let background objects move
        args.no_moving_distractors = True

        return args

    args = parser.parse_args()
    args = domino_postproc(args)
    args = postprocess(args)

    return args

class Playroom(Collision):

    def __init__(self, port=1071,
                 probe_categories=None,
                 target_categories=None,
                 **kwargs):

        self.probe_categories = probe_categories
        self.target_categories = target_categories        
        super().__init__(port=port, **kwargs)

    def set_probe_types(self, olist):
        tlist = self.get_types(olist, libraries=MODEL_LIBRARIES.keys(), categories=self.probe_categories, flex_only=False)
        self._probe_types = tlist

    def set_target_types(self, olist):
        tlist = self.get_types(olist, libraries=MODEL_LIBRARIES.keys(), categories=self.target_categories, flex_only=False)
        self._target_types = tlist

    def _get_zone_location(self, scale):
        """Where to place the target zone? Right behind the target object."""
        return TDWUtils.VECTOR3_ZERO

    def clear_static_data(self) -> None:
        Dominoes.clear_static_data(self)
        # clear some other stuff

    def _place_target_object(self) -> List[dict]:

        self._fixed_target = True
        return Dominoes._place_target_object(self, size_range=self.target_scale_range)

    def _place_and_push_probe_object(self) -> List[dict]:
        return Dominoes._place_and_push_probe_object(self, size_range=self.probe_scale_range)

    def _write_static_data(self, static_group: h5py.Group) -> None:
        Dominoes._write_static_data(self, static_group)

    @staticmethod
    def get_controller_label_funcs(classname = "Collision"):

        funcs = Dominoes.get_controller_label_funcs(classname)

        return funcs
    
    def is_done(self, resp: List[bytes], frame: int) -> bool:
        return frame > 150 # End after X frames even if objects are still moving.

    def _set_distractor_attributes(self) -> None:

        self.distractor_angular_spacing = 20
        self.distractor_distance_fraction = [0.2,0.8]
        self.distractor_rotation_jitter = 30
        self.distractor_min_z = self.middle_scale['z'] * 2.0
        self.distractor_min_size = 1.0
        self.distractor_max_size = 2.0

    def _set_occlusion_attributes(self) -> None:

        self.occluder_angular_spacing = 15
        self.occlusion_distance_fraction = [0.3,0.8]
        self.occluder_rotation_jitter = 30.
        self.occluder_min_z = self.middle_scale['z'] * 2.0
        self.occluder_min_size = 0.5
        self.occluder_max_size = 1.5
        self.rescale_occluder_height = True    
    

if __name__ == "__main__":
    import platform, os
    
    args = get_playroom_args("playroom")
    
    if platform.system() == 'Linux':
        if args.gpu is not None:
            os.environ["DISPLAY"] = ":0." + str(args.gpu)
        else:
            os.environ["DISPLAY"] = ":0"

    PC = Playroom(
        port=args.port,
        room=args.room,
        randomize=args.random,
        seed=args.seed,
        target_zone=args.zone,
        zone_location=args.zlocation,
        zone_scale_range=args.zscale,
        zone_color=args.zcolor,
        zone_material=args.zmaterial,
        zone_friction=args.zfriction,
        target_objects=args.target,
        target_categories=args.target_categories,
        probe_objects=args.probe,
        probe_categories=args.probe_categories,
        target_scale_range=args.tscale,
        target_rotation_range=args.trot,
        probe_rotation_range=args.prot,
        probe_scale_range=args.pscale,
        probe_mass_range=args.pmass,
        target_color=args.color,
        probe_color=args.pcolor,
        collision_axis_length=args.collision_axis_length,
        force_scale_range=args.fscale,
        force_angle_range=args.frot,
        force_offset=args.foffset,
        force_offset_jitter=args.fjitter,
        force_wait=args.fwait,
        remove_target=bool(args.remove_target),
        remove_zone=bool(args.remove_zone),
        zjitter = args.zjitter,
        fupforce = args.fupforce,
        ## not scenario-specific
        camera_radius=args.camera_distance,
        camera_min_angle=args.camera_min_angle,
        camera_max_angle=args.camera_max_angle,
        camera_min_height=args.camera_min_height,
        camera_max_height=args.camera_max_height,
        monochrome=args.monochrome,
        material_types=args.material_types,
        target_material=args.tmaterial,
        probe_material=args.pmaterial,
        distractor_types=args.distractor,
        distractor_categories=args.distractor_categories,
        num_distractors=args.num_distractors,
        occluder_types=args.occluder,
        occluder_categories=args.occluder_categories,
        num_occluders=args.num_occluders,
        occlusion_scale=args.occlusion_scale,
        occluder_aspect_ratio=args.occluder_aspect_ratio,
        distractor_aspect_ratio=args.distractor_aspect_ratio,                
        probe_lift = args.plift,
        flex_only=args.only_use_flex_objects,
        no_moving_distractors=args.no_moving_distractors,
        match_probe_and_target_color=args.match_probe_and_target_color        
    )

    if bool(args.run):
        PC.run(num=args.num,
                 output_dir=args.dir,
                 temp_path=args.temp,
                 width=args.width,
                 height=args.height,
                 framerate=args.framerate,
                 save_passes=args.save_passes.split(','),
                 save_movies=args.save_movies,
                 save_labels=args.save_labels,
                 save_meshes=args.save_meshes,
                 write_passes=args.write_passes,
                 args_dict=vars(args)
        )
    else:
        PC.communicate({"$type": "terminate"})
