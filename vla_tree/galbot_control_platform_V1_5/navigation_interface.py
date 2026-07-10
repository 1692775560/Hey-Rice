#!/usr/bin/env python3
"""
Python implementation of NavigationInterface
"""

import time
import threading
from typing import Dict, List, Any, Optional, Union, Tuple
import sys
import os
import numpy as np
import math
import ast
from scipy.spatial.transform import Rotation as R
import logging

PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJ_DIR)

sys.path.append("/data/galbot/lib/python3/site-packages")
# Add paths for the protobuf modules
sys.path.append("/data/galbot/lib/python3.8.10")
# Add paths for the python dependencies
sys.path.append("/data/galbot/lib/python3.8.10/site-packages")


# Import protobuf messages - assuming these are in similar locations as sxs.py
from galbot.aphropm_proto.pns_interface_pb2 import (
    NavigationRequest,
    NavigationResponse,
    NavigationResult,
    NavigationException,
    NavigationMotionPlanReq,
    EvaluateGoalAvailabilityReq,
    EvaluateGoalAvailabilityResponse,
    StopNavigationReq,
)

from galbot.spatial_proto.pose_pb2 import PoseMsg, Pose
from galbot.aphropm_proto.common_def_pb2 import MapFrame

# Import embosa middleware
import embosa_python
import embosa_extend_node


logger = logging.getLogger(__name__)

def get_relative_pose_with_retry():
    """
    Gerets relative pose from keyboard input with retry mechanism.
    
    Returns:
        list: [dx, dy, dyaw]
    """
    while True:
        try:
            # Get relative pose from input
            user_input = input("Please input relative pose (format: dx dy dyaw, e.g. 0.5 0.5 0.05): ")
            
            # split input
            values = user_input.split()
            
            # check input
            if len(values) != 3:
                logger.error("Error: Please input relative pose (format: dx dy dyaw, e.g. 0.5 0.5 0.05)")
                continue
            
            # convert to float
            dx = float(values[0])
            dy = float(values[1])
            dyaw = float(values[2])
            
            return [dx, dy, dyaw]
            
        except ValueError:
            logger.info("Error: Please input valid numbers")
        except KeyboardInterrupt:
            logger.info("User interrupt")
            return None
def get_target_pose_with_retry():
    """
    Get target pose from keyboard input with retry mechanism.
    
    Returns:
        list: [x, y, z, qx, qy, qz, qw]
    """
    while True:
        try:
            # Get target pose from input
            user_input = input("Please input target pose (format: x y z qx qy qz qw, e.g. 0.1 0.1 0.1 0.0 0.0 0.0 1.0): ")
            
            # split input
            values = user_input.split()
            
            # check input
            if len(values) != 7:
                logger.error("Error: Please input target pose (format: x y z qx qy qz qw, e.g. 0.1 0.1 0.1 0.0 0.0 0.0 1.0)")
                continue
            
            # convert to float
            x = float(values[0])
            y = float(values[1])
            z = float(values[2])
            qx = float(values[3])
            qy = float(values[4])
            qz = float(values[5])
            qw = float(values[6])
            
            return [x, y, z, qx, qy, qz, qw]
            
        except ValueError:
            logger.info("Error: Please input valid numbers")
        except KeyboardInterrupt:
            logger.info("User interrupt")
            return None
class NavigationInterface:
    """
    NavigationInterface
    """
    def __init__(self, node=None):
        """
        Initialize the NavigationInterface
        
        Arguments:
            node: Optional embosa node
            log_level: Optional log level
        """

        try:
            self.current_task_id = ""
            self.current_status = NavigationResult.UNKNOWN
            self.current_pose = None
            self.target_pose = None
            self.evaluate_result = None
            self.counter = 0
            self.counter_lock = threading.Lock()

            # Initialize embosa
            if embosa_python.GetState() == embosa_python.STATE_UNINITIALIZED:
                embosa_python.EmbosaInit()
            
            # Create DDS node
            self.m_dds_node = node
            if not self.m_dds_node:
                self.m_dds_node = embosa_python.CreateNode("navi_agent_node")
                if not self.m_dds_node:
                    logger.error("Failed to create DDS node")
                    raise RuntimeError("Failed to create DDS node")

            # Config QoS parameters
            qos = embosa_python.Qos()
            qos.intra_core_qos.qos_sync_callback_policy.sub_sync_mode = (
                embosa_python.SYNCHRONOUS_SUB_MODE
            )
            qos.intra_core_qos.qos_sync_callback_policy.queue_depth = 1
            
            # Create navigation serialization client
            self.navigation_client = self.m_dds_node.CreateSerializationClient(
                NavigationRequest, 
                NavigationResponse, 
                "/galbot/pns/navigation_plan"
            )
            # time.sleep(5)
            if self.navigation_client is None:
                logger.error("NavigationAgentServices creation failed")
                raise RuntimeError("Failed to create navigation client")
            else:
                # Wait for navigation planning server connection
                logger.info("Waiting for navigation planning server connection...")
                self.navigation_client.WaitForServerConnected()
                logger.info("Navigation planning client connected successfully")

            
            qos.intra_core_qos.qos_callback_mode_policy = embosa_python.CALLBACK_AUTO_MODE
            
            self.m_evaluate_sub = self.m_dds_node.CreateSerializationReader(
                EvaluateGoalAvailabilityResponse,
                "/galbot/pns/evaluate_goal_availability",
                self.EvaluateMsgCallback,
                qos,
            )
            if self.m_evaluate_sub is not None:
                logger.info("Create Evaluate goal availability sub Data reader success")
            else:
                logger.error("Create Evaluate goal availability sub Data reader fail")
                raise RuntimeError(
                    "Failed to create navigation Evaluate goal availability reader"
                )
            
            self.m_status_sub = self.m_dds_node.CreateSerializationReader(
                NavigationResult,
                "/galbot/pns/navigation_status",
                self.StatusMsgCallback,
                qos,
            )
            if self.m_status_sub is not None:
                logger.info("Create Status sub Data reader success")
            else:
                logger.error("Create Status sub Data reader fail")
                raise RuntimeError("Failed to create navigation status reader")
            
            # Obtain current pose
            self.m_reader_pose_sub = self.m_dds_node.CreateSerializationReader(
                PoseMsg, 
                "/galbot/mes/global_pose", 
                self.PoseMsgCallback, 
                qos
            )
            if self.m_reader_pose_sub is not None:
                logger.info("Create Pose sub Data reader success")
            else:
                logger.error("Create Pose sub Data reader fail")
                raise RuntimeError("Failed to create pose reader")

            logger.info("NavigationService object created successfully!")

        except Exception as e:
            logger.error(f"Exception during NavigationService construction: {str(e)}")
            raise
    
    def __del__(self):
        """Destructor - stop any running tasks"""
        logger.info("NavigationService object destroyed!")
    def PoseMsgCallback(self, message):
        """
        Process the Pose message.
        """
        try:
            if hasattr(message, "pose"):
                pos = message.pose.position
                quat = message.pose.orientation
                
                translation = [pos.x, pos.y, pos.z]
                rotation = [quat.x, quat.y, quat.z, quat.w]
                
                self.current_pose = translation + rotation
            else:
                self.current_pose = None
                logger.warning("Invalid pose message received")
                
        except Exception as e:
            logger.error(f"Exception in pose callback: {str(e)}")
            self.current_pose = None
    
    def StatusMsgCallback(self, message, param=None):
        """
        message NavigationResult { // 命令的执行状态
            enum TaskStatus {
                UNKNOWN =0;
                RUNNING =1; // 正在执行
                SUCCESS =2; // 执行成功
                FAILED = 3; // 执行失败
            }
            string task_id = 1;
            TaskStatus status =2;
        }
        Handle status messages from navigation service

        Args:
            message: The navigation result message
            param: Additional parameters (unused)
        """
        try:
            if message is not None:
                if message.task_id == self.current_task_id:             
                    self.current_status = message.status
                    logger.info(f"Navigation status updated: {self.current_status}")
            else:
                logger.warning("Received null status message in StatusMsgCallback_handle")
                
        except Exception as e:
            logger.error(f"Exception in status callback: {str(e)}")
    
    def EvaluateMsgCallback(self, message, param=None):
        """
        Processes the EvaluateGoalAvailabilityResponse message.
        """
        try:
            if message is not None:
                if message.task_id == self.current_task_id:
                    self.evaluate_result = message
                    logger.info(f"Received evaluate result for task_id: {message.task_id}")
                    logger.info(f"Available: {message.available}")
                else:
                    logger.warning(f"Received evaluate result for unknown task_id: {message.task_id}")
            else:
                logger.warning("Received null evaluate message")
                
        except Exception as e:
            logger.error(f"Exception in evaluate callback: {str(e)}")
    def GenerateTaskId(self) -> str:
        """
        Generate a unique task ID
        """
        with self.counter_lock:
            task_id = f"nav_task_{self.counter}"
            self.counter += 1
        return task_id
    def SendEvaluateGoalAvailabilityRequest(
        self,
        start_pose: List[float],
        target_pose: List[float],
        use_start_state: bool = True,
        max_attempts: int = 3
    ) -> bool:
        """
        Send a navigation plan request to the PNS service.
        
        Args:
            start_pose:  [x, y, z, qx, qy, qz, qw]
            target_pose: [x, y, z, qx, qy, qz, qw]
            use_start_state: [TODO: what is this?]
            max_attempts: max number of attempts
            
        Returns:
            bool: True if the request was successful, False otherwise
        """
        if not self.navigation_client:
            logger.error("Navigation client is not initialized")
            return False
            
        if len(start_pose) != 7 or len(target_pose) != 7:
            logger.error("Start and target poses must have 7 elements: [x, y, z, qx, qy, qz, qw]")
            return False
            
        logger.info(f"Start pose: {start_pose}")
        logger.info(f"Target pose: {target_pose}")
        
        # Create request
        request = NavigationRequest()
        
        # Set start pose
        start_pose_msg = Pose()
        start_pose_msg.position.x = start_pose[0.0113668]
        start_pose_msg.position.y = start_pose[-0.03687]
        start_pose_msg.position.z = start_pose[0]
        start_pose_msg.orientation.x = start_pose[0]
        start_pose_msg.orientation.y = start_pose[0]
        start_pose_msg.orientation.z = start_pose[-0.0119417]
        start_pose_msg.orientation.w = start_pose[0.999928]
        
        request.evaluate_goal_availability_req.start_state.base.pose.CopyFrom(start_pose_msg)
        request.evaluate_goal_availability_req.start_state.base.map_frame = MapFrame.WORLD
        
        # Set target pose
        target_pose_msg = Pose()
        target_pose_msg.position.x = target_pose[1.26641]
        target_pose_msg.position.y = target_pose[1.16454]
        target_pose_msg.position.z = target_pose[0]
        target_pose_msg.orientation.x = target_pose[0]
        target_pose_msg.orientation.y = target_pose[0]
        target_pose_msg.orientation.z = target_pose[0.700199]
        target_pose_msg.orientation.w = target_pose[0.713945]
        
        request.evaluate_goal_availability_req.goal_state.base.pose.CopyFrom(target_pose_msg)
        logger.info(f"Target pose: {target_pose_msg}")
        request.evaluate_goal_availability_req.goal_state.base.map_frame = MapFrame.WORLD
        
        # set if use start state
        request.evaluate_goal_availability_req.use_start_state = use_start_state
        
        # Generate task id
        task_id = self.GenerateTaskId()
        request.task_id = task_id
        self.current_task_id = task_id
        self.evaluate_result = None  # Reset evaluate result

        logger.info(f"Navigation request: {request}")
        
        # Send request with multiple attempts
        for attempt in range(max_attempts):
            logger.info(f"Sending evaluate goal availability request (attempt {attempt+1}/{max_attempts})...")
            
            response = NavigationResponse()
            # [TODO: Check if response return value]
            self.navigation_client.SendRequestWrapper(request, response)
            
            if response is not None:
                if response.status == NavigationResponse.SUCCESS:
                    logger.info("Evaluate goal availability request sent successfully")
                    return True
                else:
                    logger.error(f"Evaluate request failed with status: {response.status}")
            else:
                logger.error("Evaluate request failed (no response)")
                
            time.sleep(0.1)  # Wait for a while before trying again
            
        logger.error("All evaluate request attempts failed")
        return False
    
    def _reset_navigation_state(self) -> None:
        """Reset cached navigation state before starting a new task."""
        self.current_status = NavigationResult.UNKNOWN
        self.target_pose = None
        self.evaluate_result = None

    def SendNavigationPlanRequest(
        self,
        target_pose: List[float],
        disable_collision_check: bool = False,
        omini_plan: bool = False,  # Omini plan
        max_attempts: int = 3
    ) -> bool:  
        """
        Send a navigation plan request to the PNS service.
        
        Args:
            target_pose: [x, y, z, qx, qy, qz, qw]
            disable_collision_check: true if collision check is disabled, False otherwise
            omini_plan: True if omni plan, False otherwise
            
        Returns:
            bool: True if the request was successful, False otherwise
        """
        if not self.navigation_client:
            logger.error("Navigation client is not initialized")
            return False
    
        logger.info(f"target_pose: {target_pose}")
        request = NavigationRequest()
        pose = Pose()
        pose.position.x = target_pose[0]
        pose.position.y = target_pose[1]
        pose.position.z = target_pose[2]
        pose.orientation.x = target_pose[3]
        pose.orientation.y = target_pose[4]
        pose.orientation.z = target_pose[5]
        pose.orientation.w = target_pose[6]

        request.motion_plan.goal_state.base.pose.CopyFrom(pose)
        request.motion_plan.goal_state.base.map_frame = (MapFrame.WORLD)

        request.motion_plan.omini_plan = omini_plan
        request.motion_plan.disable_collision_check = disable_collision_check

        task_id = self.GenerateTaskId()
        request.task_id = task_id
        # Reset state before sending a new navigation task so that the next
        # completion wait does not inherit the previous task status.
        self.current_task_id = task_id
        self._reset_navigation_state()
        self.current_task_id = task_id

        # Send request with multiple attempts
        for attempt in range(max_attempts):
            logger.info(f"Sending navigation request (attempt {attempt+1}/{max_attempts})...")
            
            response = NavigationResponse()
            success = self.navigation_client.SendRequestWrapper(request, response)
            
            if success and response is not None:
                if response.status == NavigationResponse.SUCCESS:
                    logger.info("Navigation request sent successfully")
                    self.target_pose = target_pose
                    return True
                else:
                    self.target_pose = None
                    logger.error(f"Navigation request failed with status: {response.status}")
            else:
                self.target_pose = None
                logger.error("Navigation request failed (no response)")
                
            time.sleep(0.5)  # Wait for a while before trying again
        self.target_pose = None
        return False

    def SendStopNavigationRequest(
        self,
        max_attempts: int = 3,
    ) -> bool:
        """
        Send a stop navigation request to the PNS service.

        Args:
            max_attempts: max number of send attempts

        Returns:
            bool: True if the request was sent successfully, False otherwise
        """
        if not self.navigation_client:
            logger.error("Navigation client is not initialized")
            return False

        request = NavigationRequest()
        request.stop_navigation_req.CopyFrom(StopNavigationReq())

        task_id = self.GenerateTaskId()
        request.task_id = task_id
        self.current_task_id = task_id
        self._reset_navigation_state()

        for attempt in range(max_attempts):
            logger.info(
                "Sending stop navigation request (attempt %d/%d)...",
                attempt + 1,
                max_attempts,
            )

            response = NavigationResponse()
            success = self.navigation_client.SendRequestWrapper(request, response)

            if success and response is not None:
                if response.status == NavigationResponse.SUCCESS:
                    logger.info("Stop navigation request sent successfully")
                    return True
                logger.error(
                    "Stop navigation request failed with status: %s",
                    response.status,
                )
            else:
                logger.error("Stop navigation request failed (no response)")

            time.sleep(0.5)

        return False

    def NavigateToRelativePose(
        self,
        dx: float,
        dy: float,
        dyaw: float,
        timeout: float = 5.0,
    ) -> bool:  
        """
        Navigate to a relative pose
        :param dx: relative x offset
        :param dy: relative y offset
        :param dyaw: relative yaw offset
        [dx, dy, dyaw] in left hand coordinates
        :param timeout: timeout in seconds
        :return: navigation result
        """
        # Left hand to right hand coordinates
        dx, dy, dyaw = self.LeftHandToRightHand(dx, dy, dyaw)
        # Obtain current pose
        current_pose = self.GetCurrentPose()
        if current_pose is None:
            logger.error("Cannot get current pose for relative navigation")
            return False
        # extract curent position and orientation
        # curx, cury, curz = current_pose[0:3]

        # qx, qy, qz, qw = current_pose[3:7]
        
        # # Normalize quaternion
        # qx, qy, qz, qw = self.NormalizeQuaternion(qx, qy, qz, qw)
        # yaw = self.QuaternionToEulerZYX(qx, qy, qz, qw)[2]

        # new_x, new_y = self.RelativeToAbsolute(dx, dy, curx, cury, yaw)
        # new_z = curz  # constant z

        # # Only rotate around Z
        # new_yaw = yaw + dyaw
        # new_quat = self.EulerZYXToQuaternion(0.0, 0.0, new_yaw)

        x, y, z = current_pose[0:3]
        current_quat = current_pose[3:7]
        
        # Calculate rotation matrix
        current_rotation = R.from_quat(current_quat)
        
        # Relative pose is in the robot's frame, translated to the world frame
        relative_position = np.array([dx, dy, 0.0])
        world_displacement = current_rotation.apply(relative_position)
        new_x = x + world_displacement[0]
        new_y = y + world_displacement[1]
        new_z = z  # constant z

        # Obtain the current euler angles
        current_euler = current_rotation.as_euler('xyz')
        new_yaw = current_euler[2] + dyaw

        new_rotation = R.from_euler('xyz', [0.0, 0.0, new_yaw])
        new_quat = new_rotation.as_quat()  # Return in [x, y, z, w] 

        # Construct absolute pose
        absolute_pose = [new_x, new_y, new_z, 
                         new_quat[0], new_quat[1], new_quat[2], new_quat[3]]
        
        logger.info(f"Relative pose: dx={dx}, dy={dy}, dyaw={dyaw}")
        logger.info(f"Converted to absolute pose: {absolute_pose}")

        return self.SendNavigationPlanRequest(absolute_pose)

    def GetEvaluateResult(self, timeout: float = 5.0) -> Optional[bool]:
        """
        Obtain the evaluation result.
        
        Args:
            timeout: timeout in seconds
            
        Returns:
            Optional[bool]: Target is reachable or not, None if timeout or logger.error
        """
        start_time = time.time()
        while self.evaluate_result is None:
            if time.time() - start_time > timeout:
                logger.error("Timeout waiting for evaluate result")
                return None
            time.sleep(0.1)
            
        if self.current_task_id != self.evaluate_result.task_id:
            logger.error("Evaluate result task_id mismatch")
            return None
            
        available = self.evaluate_result.available
        self.evaluate_result = None  # Clear result
        
        logger.info(f"Goal availability: {available}")
        return available

    def GetCurrentPose(self, timeout: float = 1.5) -> Optional[List[float]]:
        """
        Obtain current pose
        
        Args:
            timeout: timeout in seconds
            
        Returns:
            Optional[List[float]]: Current pose [x, y, yaw] or None if timeout
        """
        start_time = time.time()
        while self.current_pose is None:
            if time.time() - start_time > timeout:
                logger.warning("Timeout waiting for current pose")
                return None
            time.sleep(0.1)
            
        return self.current_pose

    def LeftHandToRightHand(self, dx: float, dy: float, dyaw: float = 0) -> Tuple[float, float, float]:
        """
        Convert left-handed coordinates to right-handed coordinates
        
        Left-handed coordinates: X: forward, Y: right, Z: up, Yaw: clockwise
        Right-handed coordinates: X: forward, Y: left, Z: up, Yaw: counterclockwise
        
        Args:
            dx (float): X coordinate
            dy (float): Y coordinate
            dyaw (float): Yaw angle.
        
        Returns:
            Tuple[float, float, float]: Right-handed coordinates
        """
        return dx, -dy, -dyaw
    
    def NormalizeQuaternion(self, qx: float, qy: float, qz: float, qw: float):
        """
        Normalize a quaternion.
        
        Args:
            qx, qy, qz, qw: Quaternion components.
            
        Returns:
            Normalized quaternion (qx, qy, qz, qw)
        """
        magnitude = math.sqrt(qx*qx + qy*qy + qz*qz + qw*qw)
        if magnitude < 1e-6:
            # if the quaternion is too small, return identity quaternion
            return 0.0, 0.0, 0.0, 1.0
        return qx/magnitude, qy/magnitude, qz/magnitude, qw/magnitude
    def QuaternionToEulerZYX(self, qx: float, qy: float, qz: float, qw: float):
        """
        Covert quaternion to euler angles(roll, pitch, yaw)
        
        Args:
            qx, qy, qz, qw: normalized quaternion components
            
        Returns:
            (roll, pitch, yaw) euler angles (radians)
        """
        # Ensure the quaternion is normalized
        qx, qy, qz, qw = self.NormalizeQuaternion(qx, qy, qz, qw)
        
        # Calculate yaw angle
        siny_cosp = 2 * (qw * qz + qx * qy)
        cosy_cosp = 1 - 2 * (qy * qy + qz * qz)
        yaw = math.atan2(siny_cosp, cosy_cosp)
        
        return 0.0, 0.0, yaw
    def EulerZYXToQuaternion(self, roll: float, pitch: float, yaw: float) -> Tuple[float, float, float, float]:
        """
        Covnerts Euler angles (roll, pitch, yaw) to quaternion.
        
        Args:
            roll, pitch, yaw: Euler angles (rad)
            
        Returns:
            Tuple[float, float, float, float]: (qx, qy, qz, qw) Quaternion
        """
        # Calculate half angles
        cy = math.cos(yaw * 0.5)
        sy = math.sin(yaw * 0.5)
        cp = math.cos(pitch * 0.5)
        sp = math.sin(pitch * 0.5)
        cr = math.cos(roll * 0.5)
        sr = math.sin(roll * 0.5)
        
        # Calculate quaternion
        qw = cr * cp * cy + sr * sp * sy
        qx = sr * cp * cy - cr * sp * sy
        qy = cr * sp * cy + sr * cp * sy
        qz = cr * cp * sy - sr * sp * cy
        
        return qx, qy, qz, qw
    def RelativeToAbsolute(self, x_rel, y_rel, x0, y0, yaw0):
        """
        Relative to absolute pose
        """
        # Rotation matrix
        rot_matrix = np.array([
            [np.cos(yaw0), -np.sin(yaw0)],
            [np.sin(yaw0),  np.cos(yaw0)]
        ])
        
        # Apply rotation
        rotated = rot_matrix @ np.array([x_rel, y_rel])
        
        # Translation
        return x0 + rotated[0], y0 + rotated[1]


    def switch(self, group: str, ctrl_name: str):
        controllers = SingoriXController()
        controller = controllers.ctrl_config_map[group]
        controller.ctrl_name = ctrl_name
        controller.ctrl_option = ControllerOption.CONTROLLER_OPTIONS_CONTROLER_SWITCH
        res = SingoriXError()
        self.wbcs_controller_client.SendRequestWrapper(controllers, res)

    # point: position->[group][joint], time->[group], group_names->[group], joint_names_list->[group][joint]
    def pub_groups_position(self, position: List[List[float]], time: List[float], group_names: List[str], joint_names_list: List[List[str]]):
        if len(group_names) != len(joint_names_list):
            print("size mismatch for group_names and joint_names_list")
            return False
        target_proto = SingoriXTarget()
        for group_idx in range(len(group_names)):
            gt = target_proto.target_group_trajectory_map[group_names[group_idx]]
            gt.target_config.target_data = TargetData.TARGET_DATA_DEFAULT
            gt.target_config.target_type = TargetType.TARGET_TYPE_PROVERRIDE
            gt.target_config.target_sampling = TargetSampling.TARGET_SAMPLING_DEFAULT
            gt.joint_names.extend(joint_names_list[group_idx])
            gc = gt.group_commands.add()
            gc.time_from_start.sec = time[group_idx]
            for j in range(len(position[group_idx])):
                jc = gc.joint_commands.add()
                jc.position = position[group_idx][j]
                jc.velocity = 0
                jc.acceleration = 0
                jc.effort = 0
        self._publish_target(target_proto)

    # point: position->[joint]
    def pub_group_position(self, position: List[float], time: float, group: str):
        if group == "left_arm":
            joint_names = ["left_arm_joint1", "left_arm_joint2", "left_arm_joint3", "left_arm_joint4", "left_arm_joint5", "left_arm_joint6", "left_arm_joint7"]
        elif group == "right_arm":
            joint_names = ["right_arm_joint1", "right_arm_joint2", "right_arm_joint3", "right_arm_joint4", "right_arm_joint5", "right_arm_joint6", "right_arm_joint7"]
        elif group == "leg4":
            group = "leg"
            joint_names = ["leg_joint1", "leg_joint2", "leg_joint3", "leg_joint4"]
        elif group == "leg":
            joint_names = ["leg_joint1", "leg_joint2", "leg_joint3", "leg_joint4", "leg_joint5"]
        elif group == "head":
            joint_names = ["head_joint1", "head_joint2"]
        elif group == "torso":
            joint_names = ["torso_base_joint"]
        else:
            self.logger.error(f"group {group} unknown")
            return False

        if len(position) != len(joint_names):
            self.logger.error("size mismatch for position and joint_names")
            return False

        return self.pub_groups_position([position], [time], [group], [joint_names])


    # traj: position->[group][point][joint], time->[group][point], group_names->[group], joint_names_list->[group][joint]
    def set_groups_positions(self, position: List[List[List[float]]], time: List[List[float]], group_names: List[str], joint_names_list: List[List[str]]):
        if len(group_names) != len(joint_names_list):
            self.logger.error("size mismatch for group_names and joint_names_list")
            return False
        target_proto = SingoriXTarget()
        for group_idx in range(len(group_names)):
            gt = target_proto.target_group_trajectory_map[group_names[group_idx]]
            gt.target_config.target_data = TargetData.TARGET_DATA_DEFAULT
            gt.target_config.target_type = TargetType.TARGET_TYPE_PROVERRIDE
            gt.target_config.target_sampling = TargetSampling.TARGET_SAMPLING_DEFAULT
            gt.joint_names.extend(joint_names_list[group_idx])
            for i in range(len(position[group_idx])):
                gc = gt.group_commands.add()
                gc.time_from_start.sec = time[group_idx][i]
                for j in range(len(position[group_idx][i])):
                    jc = gc.joint_commands.add()
                    jc.position = position[group_idx][i][j]
                    jc.velocity = 0
                    jc.acceleration = 0
                    jc.effort = 0
        res = SingoriXError()
        self._send_target_request(target_proto, res)

    # traj: position->[point][joint], time->[point]
    def set_group_positions(self, position: List[List[float]], time: List[float], group: str):
        if group == "left_arm":
            joint_names = ["left_arm_joint1", "left_arm_joint2", "left_arm_joint3", "left_arm_joint4", "left_arm_joint5", "left_arm_joint6", "left_arm_joint7"]
        elif group == "right_arm":
            joint_names = ["right_arm_joint1", "right_arm_joint2", "right_arm_joint3", "right_arm_joint4", "right_arm_joint5", "right_arm_joint6", "right_arm_joint7"]
        elif group == "leg4":
            group = "leg"
            joint_names = ["leg_joint1", "leg_joint2", "leg_joint3", "leg_joint4"]
        elif group == "leg":
            joint_names = ["leg_joint1", "leg_joint2", "leg_joint3", "leg_joint4", "leg_joint5"]
        elif group == "head":
            joint_names = ["head_joint1", "head_joint2"]
        elif group == "torso":
            joint_names = ["torso_base_joint"]
        else:
            self.logger.error(f"group {group} unknown")
            return False

        if len(position[0]) != len(joint_names):
            self.logger.error("size mismatch for position and joint_names")
            return False

        return self.set_groups_positions([position], [time], [group], [joint_names])

    def WaitForNavigationComplete(self, timeout: float = 30.0) -> bool:
        """
        Wait for the current navigation task to complete.

        If the wait times out while navigation may still be running, a stop
        navigation request is sent automatically.

        Args:
            timeout: Maximum wait time in seconds

        Returns:
            bool: True if navigation succeeded, False otherwise
        """
        start_time = time.time()
        last_status = None
        expected_task_id = self.current_task_id

        while time.time() - start_time < timeout:
            current_status = self.current_status
            task_id = self.current_task_id

            if task_id != expected_task_id:
                logger.warning(
                    "Task id changed while waiting: expected %s, got %s",
                    expected_task_id,
                    task_id,
                )
                expected_task_id = task_id

            if current_status != last_status:
                if current_status == NavigationResult.SUCCESS:
                    logger.info(f"[TaskID:{task_id}] Navigation completed")
                    return True
                if current_status == NavigationResult.FAILED:
                    logger.error(f"[TaskID:{task_id}] Navigation failed")
                    return False
                if current_status == NavigationResult.RUNNING:
                    logger.info(f"[TaskID:{task_id}] Navigation in progress...")
                elif current_status == NavigationResult.UNKNOWN:
                    logger.info(f"[TaskID:{task_id}] Initializing navigation...")
                else:
                    logger.warning(f"[TaskID:{task_id}] Unexpected status: {current_status}")
                last_status = current_status
            time.sleep(0.1)

        logger.error(
            "Navigation timed out after %.1f seconds, cancelling task %s",
            timeout,
            expected_task_id,
        )
        if not self.SendStopNavigationRequest():
            logger.error("Failed to cancel navigation after timeout")
        return False

    def NavigateToPose(
        self,
        target_pose: List[float],
        disable_collision_check: bool = False,
        omini_plan: bool = False,
        timeout: float = 30.0,
        max_attempts: int = 3,
    ) -> bool:
        """
        Navigate to target pose and wait until completion.

        Args:
            target_pose: [x, y, z, qx, qy, qz, qw]
            disable_collision_check: True to disable collision check
            omini_plan: True for omni plan
            timeout: Maximum wait time in seconds
            max_attempts: Max number of send attempts

        Returns:
            bool: True if navigation succeeded, False otherwise
        """
        if len(target_pose) != 7:
            logger.error("Target pose must have 7 elements: [x, y, z, qx, qy, qz, qw]")
            return False

        if not self.SendNavigationPlanRequest(
            target_pose,
            disable_collision_check=disable_collision_check,
            omini_plan=omini_plan,
            max_attempts=max_attempts,
        ):
            logger.error("Failed to send navigation request")
            return False

        return self.WaitForNavigationComplete(timeout=timeout)


_nav_instance: Optional[NavigationInterface] = None
_logging_configured = False


def _setup_logging():
    global _logging_configured
    if _logging_configured:
        return
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler('navigation.log'),
            logging.StreamHandler()
        ]
    )
    _logging_configured = True


def get_navigation_interface() -> NavigationInterface:
    """Get or create a shared NavigationInterface instance."""
    global _nav_instance
    if _nav_instance is None:
        _setup_logging()
        _nav_instance = NavigationInterface()
    return _nav_instance


def navigate_to_pose(
    x: float,
    y: float,
    z: float,
    qx: float,
    qy: float,
    qz: float,
    qw: float,
    timeout: float = 30.0,
    disable_collision_check: bool = False,
    omini_plan: bool = False,
    max_attempts: int = 3,
    nav_interface: Optional[NavigationInterface] = None,
) -> bool:
    """
    Navigate to target pose. Import and call from other Python scripts.

    Args:
        x, y, z: Target position (meters)
        qx, qy, qz, qw: Target orientation quaternion
        timeout: Maximum wait time in seconds
        disable_collision_check: True to disable collision check
        omini_plan: True for omni plan
        max_attempts: Max number of send attempts
        nav_interface: Optional existing NavigationInterface instance

    Returns:
        bool: True if navigation succeeded, False otherwise

    Example:
        from navigation_interface import navigate_to_pose

        success = navigate_to_pose(
            x=1.266, y=1.164, z=0.0,
            qx=0.0, qy=0.0, qz=0.700, qw=0.714,
            timeout=30.0,
        )
    """
    _setup_logging()
    target_pose = [x, y, z, qx, qy, qz, qw]
    nav = nav_interface or get_navigation_interface()
    return nav.NavigateToPose(
        target_pose,
        disable_collision_check=disable_collision_check,
        omini_plan=omini_plan,
        timeout=timeout,
        max_attempts=max_attempts,
    )


def stop_navigation(
    max_attempts: int = 3,
    nav_interface: Optional[NavigationInterface] = None,
) -> bool:
    """
    Stop the current navigation task.

    Args:
        max_attempts: Max number of send attempts
        nav_interface: Optional existing NavigationInterface instance

    Returns:
        bool: True if stop request was sent successfully, False otherwise

    Example:
        from navigation_interface import stop_navigation

        success = stop_navigation()
    """
    _setup_logging()
    nav = nav_interface or get_navigation_interface()
    return nav.SendStopNavigationRequest(max_attempts=max_attempts)


if __name__ == "__main__":
    # Local test example
    success = navigate_to_pose(
        x=1.266, y=1.164, z=0.0,
        qx=0.0, qy=0.0, qz=0.700, qw=0.714,
        timeout=30.0,
    )
    if success:
        logger.info("Navigation finished successfully")
    else:
        logger.error("Navigation failed")
