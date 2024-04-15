import numpy as np
import argparse
import yaml
from tqdm import tqdm
import matplotlib.pyplot as plt
import os
from os.path import expanduser
from copy import deepcopy
import pickle
import json

from motlee.mot.multi_object_tracker import MultiObjectTracker as MOT
from motlee.mot.motion_model import MotionModel
from motlee.tcaff.tcaff_manager import TCAFFManager
from motlee.utils.transform import xypsi_2_transform
from motlee.realign.object_map import ObjectMap
from motlee.realign.frame_aligner import FrameAlignSolution

from robot_utils.robot_data.robot_data import NoDataNearTimeException
from robot_utils.robot_data.pose_data import PoseData
from robot_utils.transform import T_FLURDF, T_RDFFLU, transform, transform_2_xytheta, T3d_2_T2d
from plot_utils import square_equal_aspect, plot_pose2d

try:
    from .motlee_data_processor import MOTLEEDataProcessor
    from .robot import Robot
    from .detections import DetectionData
    from .alignment_results import AlignmentResults
    from .rover_artist import RoverArtist
except:
    from motlee_data_processor import MOTLEEDataProcessor
    from robot import Robot
    from detections import DetectionData
    from alignment_results import AlignmentResults
    from rover_artist import RoverArtist

def recursive_dict_set(d, params):
    if type(d) == dict:
        for key, value in d.items():
            params[key] = recursive_dict_set(value, params[key])
        return params
    else:
        return d

def main(args):

    ###################################################
    #                      Setup                      #
    ###################################################

    plt.style.use('/home/masonbp/computer/python/matplotlib/publication.mplstyle')
    # plt.rcParams.update({'font.size': 20})
    
    # Load the parameters
    with open(args.params, "r") as f:
        params = yaml.full_load(f)
        
    if args.override_params is not None:
        override_params = json.loads(args.override_params)
        params = recursive_dict_set(override_params, params)
        
    robot_names = params['robots']
    for name in robot_names:
        params[name] = params[name]
    robots = []

    pose_gt_data = dict()

    if args.load_pickle is not None:
        pkl_file = open(args.load_pickle, 'rb')
        data = pickle.load(pkl_file)
        robots = data['robots']
        pose_gt_data = data['pose_gt_data']
    else:
        for name in robot_names:

            pose_data = dict()
            for pose_type in ['pose_estimate', 'pose_gt']:
                if 'frame' in params[pose_type] and params[pose_type]['frame'] == 'RDF':
                    T_postmultiply = T_RDFFLU
                elif 'T_postmultiply' in params[pose_type]:
                    T_postmultiply = np.linalg.inv(np.array(params[pose_type]['T_postmultiply'][name]).reshape((4,4)))
                else:
                    T_postmultiply = None
                pose_data[pose_type] = PoseData(
                    data_file=f"{params[pose_type]['root']}/{name}.bag",
                    file_type='bag',
                    topic=f"/{name}/{params[pose_type]['topic']}",
                    time_tol=10.,
                    interp=True,
                    T_postmultiply=T_postmultiply,
                )

            pose_est_data = pose_data['pose_estimate']
            pose_gt_data[name] = pose_data['pose_gt']

            dim = params['mapping']['dim']
            Q_el = params['mapping']['Q_el']
            P0_el = params['mapping']['P0_el']
            R_el = params['mapping']['R_el']
            Q = np.diag([*[Q_el for _ in range(dim-2)], params['mapping']['Q_el_w'], params['mapping']['Q_el_h']])
            P0 = np.diag([*[P0_el for _ in range(dim-2)], params['mapping']['P0_el_w'], params['mapping']['P0_el_h']])
            R = np.diag([R_el for _ in range(dim)])
            landmark_model = MotionModel(A=np.eye(dim), H=np.eye(dim), Q=Q, R=np.array([]), P0=P0)
            other_robots = [other_name for other_name in robot_names if other_name != name]

            mapper = MOT(
                camera_id=name,
                connected_cams=other_robots,
                track_motion_model=landmark_model,
                tau_global=0.,
                tau_local=params['mapping']['tau'],
                alpha=2000,
                kappa=params['mapping']['kappa'],
                nu=params['mapping']['nu'],
                track_storage_size=1,
                dim_association=dim
            )
            
            tcaff_manager = TCAFFManager(
                prob_no_match=params['tcaff']['prob_no_match'],
                exploring_branching_factor=params['tcaff']['exploring_branching_factor'],
                window_len=params['tcaff']['window_len'],
                max_branch_exp=params['tcaff']['max_branch_exp'],
                max_branch_main=params['tcaff']['max_branch_main'],
                rho=params['tcaff']['rho'],
                clipper_epsilon=params['tcaff']['clipper_epsilon'],
                clipper_sigma=params['tcaff']['clipper_sigma'],
                clipper_mult_repeats=params['tcaff']['clipper_mult_repeats'],
                max_obj_width=params['tcaff']['max_obj_width'],
                h_diff=params['tcaff']['h_diff'],
                wh_scale_diff=params['tcaff']['wh_scale_diff'],
                num_objs_req=params['tcaff']['num_objs_req'],
                max_opt_fraction=params['tcaff']['max_opt_fraction'],
                steps_before_main_tree_deletion=params['tcaff']['steps_before_main_tree_deletion'],
                main_tree_obj_req=params['tcaff']['main_tree_obj_req']
            )
            
            fastsam3d_detections = DetectionData(
                data_file=f"{params['fastsam3d_data']['root']}/{name}.json",
                file_type='json',
                time_tol=0.1,
                # T_BC=np.eye(4) if pose_estimate_file_type == 'bag' else T_FLURDF,
                T_BC=T_FLURDF if 'T_BC' not in params[name] else np.array(params[name]['T_BC']).reshape((4,4)),
                zmin=params['mapping']['zmin'],
                zmax=params['mapping']['zmax'],
                R=R,
                dim=dim
            )

            robots.append(Robot(
                name=name,
                neighbors=other_robots,
                pose_est_data=pose_est_data,
                mapper=mapper,
                frame_align_filters={other_name: deepcopy(tcaff_manager) for other_name in other_robots},
                mot=None,
                fastsam3d_detections=fastsam3d_detections,
                person_detector_3d_detections=None
            ))
        if args.save_pickle is not None:
            data = {'robots': robots, 'pose_gt_data': pose_gt_data}
            pkl_file = open(args.save_pickle, 'wb')
            pickle.dump(data, pkl_file)
            pkl_file.close()
            exit(0)

    # Reset timing
    if 't_start' in params:
        t0 = params['t_start']
    else:
        t0 = np.max([robot.pose_est_data.t0 for robot in robots])
    if 't_duration' in params:
        tf = t0 + params['t_duration']
    else:
        tf = np.min([robot.pose_est_data.tf for robot in robots])
        
    ###################################################
    #                Run MOTLEE                       #
    ###################################################

    # Create the data processor
    motlee_data_processor = MOTLEEDataProcessor(
        robots=robots,
        mapping_ts=params['mapping']['ts'],
        frame_align_ts=params['tcaff']['ts'],
        perform_mot=False
    )

    # setup results
    results = {r1.name: {r2.name: AlignmentResults() for r2 in robots if r1 != r2} for r1 in robots}
    # if args.viz:
    #     object_file = "/home/masonbp/data/motlee_jan_2024/objects.txt"
    #     objects = []
    #     with open(object_file, "r") as f:
    #         for l in f.readlines():
    #             objects.append([float(n.strip()) for n in l.strip().split(',')])
    #     objects = np.array(objects)
        
    # set up visualizer
    # if args.viz:
    #     xlim = [np.inf, -np.inf]
    #     ylim = [np.inf, -np.inf]
    #     for robot in robots:
    #         for t in np.arange(t0, tf, params['mapping']['ts']):
    #             T_WB = pose_gt_data[robot.name].T_WB(t)
    #             xlim = [min(xlim[0], T_WB[0,3]), max(xlim[1], T_WB[0,3])]
    #             ylim = [min(ylim[0], T_WB[1,3]), max(ylim[1], T_WB[1,3])]
    #     xlim = [xlim[0] - 10.0, xlim[1] + 10.0]
    #     ylim = [ylim[0] - 10.0, ylim[1] + 10.0]
        
    # Run the data processor
    T_errs = []
    printed_err = {robot.name: False for robot in robots}

    # if not args.terse:
    #     print(f"initial relative pose:")
    #     print(transform_2_xytheta(np.linalg.inv(pose_gt_data[robots[0].name].T_WB(t0)) @ pose_gt_data[robots[1].name].T_WB(t0)))

    #     for i, j in [(0,1), (1,0)]:
    #         for t in [t0, tf]:
    #             xytheta_ij_gt = results[robots[i].name].get_Tij_gt(t, pose_gt_data[robots[i].name], 
    #                         pose_gt_data[robots[j].name], robots[i].pose_est_data, robots[j].pose_est_data)
    #             print(xytheta_ij_gt)
            
    # for i in range(2):
    #     fig, ax = plt.subplots()
    #     robots[i].pose_est_data.T_premultiply = pose_gt_data[robots[i].name].T_WB(t0) @ np.linalg.inv(robots[i].pose_est_data.T_WB(t0)) @ robots[i].pose_est_data.T_premultiply
    #     robots[i].pose_est_data.plot2d(t0=t0, tf=tf, dt=.5)
    #     pose_gt_data[robots[i].name].plot2d(t0=t0, tf=tf, dt=.5)
    #     plt.show()


    for t in tqdm(np.arange(t0, tf, params['mapping']['ts']/10)):
        motlee_data_processor.update(t)
        if motlee_data_processor.fa_updated:
            # print(robots[0].pose_est_data.T_WB(t)[:3,3])
            # print(robots[1].pose_est_data.T_WB(t)[:3,3])
            # if args.viz:
            #     plt.gca().clear()
            #     plt.gcf().set_size_inches(10.,10.)
            #     # plt.plot(objects[:,0], objects[:,1], 'k.')
            #     r0_map = robots[0].get_map()
            #     r1_map = robots[1].get_map()
            #     Tij_gt = xypsi_2_transform(*results[robots[0].name].get_Tij_gt(t, pose_gt_data[robots[0].name], 
            #             pose_gt_data[robots[1].name], robots[0].pose_est_data, robots[1].pose_est_data))
            #     if len(r0_map) > 0:
            #         T = pose_gt_data[robots[0].name].T_WB(t) @ np.linalg.inv(robots[0].pose_est_data.T_WB(t))
            #         r0_map.centroids = transform(T, r0_map.centroids)
            #     if len(r1_map) > 0:
            #         T = pose_gt_data[robots[1].name].T_WB(t) @ np.linalg.inv(robots[1].pose_est_data.T_WB(t))
            #         r1_map.centroids = transform(T, r1_map.centroids)
            #     if len(r0_map) > 0:
            #         plt.plot(r0_map.centroids[:,0], r0_map.centroids[:,1], 'r.')
            #     if len(r1_map) > 0:
            #         plt.plot(r1_map.centroids[:,0], r1_map.centroids[:,1], 'b.')
            #     # square_equal_aspect()
            #     plt.gca().set_aspect('equal')
            #     plt.xlim(xlim)
            #     plt.ylim(ylim)
            #     plot_pose2d(pose_gt_data[robots[0].name].T_WB(t), axis_len=1.)
            #     plot_pose2d(pose_gt_data[robots[0].name].T_WB(t0) @ np.linalg.inv(robots[0].pose_est_data.T_WB(t0)) @ robots[0].pose_est_data.T_WB(t), axis_len=1.)
            #     plot_pose2d(pose_gt_data[robots[1].name].T_WB(t), axis_len=1.)
            #     plot_pose2d(pose_gt_data[robots[1].name].T_WB(t0) @ np.linalg.inv(robots[1].pose_est_data.T_WB(t0)) @ robots[1].pose_est_data.T_WB(t), axis_len=1.)
            #     plt.pause(0.01)

            for i, ego_name in enumerate(robot_names):
                for j, other_name in enumerate(robot_names):
                    
                    if ego_name == other_name:
                        continue
                    try:
                        xytheta_ij_gt = results[ego_name][other_name].get_Tij_gt(t, pose_gt_data[ego_name], 
                            pose_gt_data[other_name], robots[i].pose_est_data, robots[j].pose_est_data)
                    except NoDataNearTimeException:
                        xytheta_ij_gt = np.zeros(3) * np.nan
                    # Tij_gt = results[ego_name].get_Tij_gt(t, pose_gt_data[ego_name], 
                    #     pose_gt_data[other_name], robots[i].pose_est_data, robots[j].pose_est_data, xytheta=False)
                    # print(Tij_gt)
                    results[ego_name][other_name].update_from_tcaff_manager(t, robots[i].frame_align_filters[other_name], xytheta_ij_gt)
                    if not np.any(np.isnan(robots[i].frame_align_filters[other_name].T)):
                        T_oi_ri = robots[i].pose_est_data.T_WB(t)
                        T_oj_rj = robots[i-1].pose_est_data.T_WB(t)
                        T_oi_oj = robots[i].frame_align_filters[other_name].T
                        T_ri_rj_est = np.linalg.inv(T_oi_ri) @ T_oi_oj @ T_oj_rj
                        # print(T_ri_rj_est)

                        try:
                            T_w_ri = pose_gt_data[ego_name].T_WB(t)
                            T_w_rj = pose_gt_data[robots[i-1].name].T_WB(t)
                            T_ri_rj_gt = np.linalg.inv(T_w_ri) @ T_w_rj
                            # print(T_ri_rj_gt)

                            T_errs.append(np.linalg.inv(T_ri_rj_gt) @ T_ri_rj_est)
                        except:
                            continue

            # for robot in robots:
            #     if not printed_err[robot.name] and not np.isnan(np.nanmean(results[robot.name].errors_translation)) and not args.terse:
            #         print(f"initial translation error (m): {np.nanmean(results[robot.name].errors_translation)}")
            #         print(f"initial rotation error (deg): {np.rad2deg(np.nanmean(results[robot.name].errors_rotation))}")
            #         printed_err[robot.name] = True

    ###################################################
    #                    Results                      #
    ###################################################

    avg_trans_err = np.mean([np.linalg.norm(T_err[:2,3]) for T_err in T_errs])
    avg_rot_err = np.mean(np.abs([transform_2_xytheta(T_err)[2] for T_err in T_errs]))
    if not args.terse:
        print(f"average translation error (m): {avg_trans_err}")
        print(f"average rotation error (deg): {np.rad2deg(avg_rot_err)}")

    print("Final results:")
    if np.any(np.isnan([np.nanmean(results[r1.name][r2.name].errors_rotation) for r1 in robots for r2 in robots if r1 != r2])):
        print("Alignment failed for at least one robot")
    else:
        # print([np.nanmean(results[r1.name][r2.name].errors) for r1 in robots for r2 in robots if r1 != r2])
        # print([np.nanmean(results[r1.name][r2.name].errors_rotation) for r1 in robots for r2 in robots if r1 != r2])
        errors_translations = np.array([np.nanmean(results[r1.name][r2.name].errors_translation) for r1 in robots for r2 in robots if r1 != r2]).reshape(-1)
        errors_rotations = np.array([np.nanmean(results[r1.name][r2.name].errors_rotation) for r1 in robots for r2 in robots if r1 != r2]).reshape(-1)
        print(f"average translation error (m): {np.nanmean(errors_translations)}")
        print(f"standard deviation of translation error (m): {np.nanstd(errors_translations)}")
        print(f"average rotation error (deg): {np.rad2deg(np.nanmean(errors_rotations))}")
        print(f"standard deviation of rotation error (deg): {np.rad2deg(np.nanstd(errors_rotations))}")

    for r1 in robots:
        for r2 in robots:
            if r1 == r2:
                continue
        
            width = 3.487
            height = .6*3.487
            fig, ax = plt.subplots(3, 1, figsize=(width,height))
            fig, ax = results[r1.name][r2.name].plot(line_kwargs={'linewidth': 1.0}, marker_kwargs={'markersize': 1.0}, figax=(fig, ax))
            fig.subplots_adjust(
                top=0.99,#0.92,
                bottom=0.08,#0.01,
                left=0.1,#0.01,
                right=0.99)
            if args.output is not None:
                file_extension = args.output.split('.')[-1]
                output_file = f"{args.output[:-(len(file_extension)+1)]}_{r1.name}_{r2.name}.{file_extension}"
                plt.savefig(output_file, transparent=False, dpi=400)
                pkl_path = f"{args.output[:-(len(file_extension)+1)]}_{r1.name}_{r2.name}.pkl"
                pkl_file = open(pkl_path, 'wb')
                pickle.dump([fig, ax], pkl_file, -1)
                pkl_file.close()
            elif not args.terse:
                plt.show()
          
    
            
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--params", "-p", type=str, required=True)
    parser.add_argument("--viz", "-v", action="store_true")
    parser.add_argument("--save-pickle", "-s", type=str, default=None, help="Save data to pickle file for faster data loading")
    parser.add_argument("--load-pickle", "-l", type=str, default=None, help="Load data from pickle file for faster data loading")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output file for plotting results")
    parser.add_argument("--override-params", "-x", type=str, default=None, help="Override parameters in the yaml file")
    parser.add_argument("--terse", "-t", action="store_true", help="Print only the average translation and rotation error")

    args = parser.parse_args()
    
    main(args)