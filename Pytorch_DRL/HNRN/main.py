from collections import defaultdict
from tqdm import tqdm
import numpy as np
import sys
import time
import rospy
import pickle
import copy

from gazebo_drl_env.srv import SimpleCtrl
from gazebo_drl_env.msg import control_group_msgs
from gazebo_drl_env.msg import state_group_msgs
from gazebo_drl_env.msg import control_msgs
from gazebo_drl_env.msg import state_msgs

from cv_bridge import CvBridge
# remove the ros packages path, or cv2 can not work
sys.path.remove('/opt/ros/kinetic/lib/python2.7/dist-packages') 
import cv2

from HNRN import HNRN
import utils

## ------------------------------

# 1 - train target driven actor
# 2 - train collision avoidance actor
# 3 - differential driver
TRAIN_TYPE = 2

TIME_DELAY = 5
STATE_DIM = 2
SENSOR_DIM = 360
ACTION_DIM = 2
TARGET_THRESHOLD = 0.01
AGENT_NUMBER = 4

MAX_EPISODES = 100
MAX_STEPS = 30


## ------------------------------

# noise parameters
mu = 0 
theta = 2
sigma = 0.2
# learning rate
actor_lr = 1e-4
critic_lr = 1e-3
batch_size = 256
# update parameters
gamma = 0.99
tau = 0.001
hmm_state = 10

## ------------------------------

# define model
model = HNRN(max_buffer=0, state_dim=STATE_DIM, sensor_dim=SENSOR_DIM, action_dim=ACTION_DIM, 
             mu=mu, theta=theta, sigma=sigma, gamma=gamma, tau=tau, train_type=TRAIN_TYPE,
             actor_lr=actor_lr, critic_lr=critic_lr, batch_size=batch_size, hmm_state=hmm_state)

# define ROS service client and messages
rospy.wait_for_service('/gazebo_env_io/pytorch_io_service')
pytorch_io_service = rospy.ServiceProxy('/gazebo_env_io/pytorch_io_service', SimpleCtrl)
TERMINAL_REWARD, COLLISION_REWARD, SURVIVE_REWARD = utils.get_parameters('../../gazebo_drl_env/param/env.yaml')

# load parameters of pre-trained model
model.load_models()
model.copy_weights()
model.load_hmm()
model.noise.reset()

print('[*] State  Dimensions : {}'.format(STATE_DIM))
print('[*] Sensor Dimensions : {}'.format(SENSOR_DIM))
print('[*] Action Dimensions : {}'.format(ACTION_DIM))
print('[*] Agent  Number : {}'.format(AGENT_NUMBER))
print('\n==================================== Start Navigation ====================================')

# save trajectory
agent_trajectory = defaultdict(list)
agent_keypoint = defaultdict(list)
first_time = 1

success_time = 0
collision_time = 0
#pbar = tqdm(total=MAX_EPISODES*MAX_STEPS)
for i_episode in range(MAX_EPISODES):

    # placeholder for states
    all_controls = control_group_msgs()
    all_current_states = state_group_msgs()
    all_next_states = state_group_msgs()
    temp_state = control_msgs()
    # inistalize current and next states
    all_current_states, all_next_states = utils.initialze_all_states_var(temp_state, all_current_states, all_next_states, AGENT_NUMBER)

    # for every episode, reset environment first. Make sure getting the right position.
    # All control messages are setted to 0.0 at the first time, and retains the value unless get new control commond for that agent.
    for i in range(AGENT_NUMBER):
        # control message should be created every time 
        current_control = control_msgs()
        current_control.linear_x = 0.0
        current_control.angular_z = 0.0
        current_control.reset = True
        all_controls.group_control.append(current_control)
    pytorch_io_service(all_controls)
    
    # after reseting, publish a new action for all agents
    for i in range(AGENT_NUMBER):
        all_controls.group_control[i].reset = False
    resp_ = pytorch_io_service(all_controls)
    time.sleep(0.2)

    terminate_flag = np.zeros(AGENT_NUMBER)
    survive_time = np.zeros(AGENT_NUMBER)
    reset_coldtime = np.zeros(AGENT_NUMBER)

    # interacting with the environment
    for i_step in range(MAX_STEPS):

        # get actions for current states
        for i_agents in range(AGENT_NUMBER):
            # update current state
            all_current_states.group_state[i_agents] = copy.deepcopy(resp_.all_group_states.group_state[i_agents])
            all_controls.group_control[i_agents].linear_x, all_controls.group_control[i_agents].angular_z = \
            model.navigation(current_state=all_current_states.group_state[i_agents])
            all_controls.group_control[i_agents].reset = False
            reset_coldtime[i_agents] = 0
            if first_time == 1:
                agent_keypoint[i_agents].append([all_current_states.group_state[i_agents].current_x, all_current_states.group_state[i_agents].current_y])


        # implement action and get response during some time
        # when one agent is terminated, start a new step after reseting
        for t in range(TIME_DELAY):
            resp_ = pytorch_io_service(all_controls)
            all_controls = utils.check_reset_flag(all_controls, AGENT_NUMBER)
            for i_agents in range(AGENT_NUMBER):
                # update all states
                if (terminate_flag[i_agents] == 0):
                    all_next_states.group_state[i_agents] = copy.deepcopy(resp_.all_group_states.group_state[i_agents])

                # check terminal flags
                if all_next_states.group_state[i_agents].terminal and reset_coldtime[i_agents] == 0:
                    reset_coldtime[i_agents] = 1
                    terminate_flag[i_agents] = 1
                    all_controls.group_control[i_agents].reset = True
                    if all_next_states.group_state[i_agents].reward == TERMINAL_REWARD:
                        success_time += 1
                    if all_next_states.group_state[i_agents].reward == COLLISION_REWARD:
                        collision_time += 1
                
                if first_time == 1:
                    agent_trajectory[i_agents].append([all_next_states.group_state[i_agents].current_x, all_next_states.group_state[i_agents].current_y])

            time.sleep(0.1)
        resp_ = pytorch_io_service(all_controls) # make sure reset operation has been done

        # check if one agent start a new loop
        for i_agents in range(AGENT_NUMBER):
            if terminate_flag[i_agents] == 1:
                terminate_flag[i_agents] = 0 # reset flag

    if first_time == 1:
        dbfile = open('figures_utils/trajectory', 'w')
        pickle.dump(agent_trajectory, dbfile)
        dbfile.close()    
        dbfile = open('figures_utils/keypoint', 'w')
        pickle.dump(agent_keypoint, dbfile)
        dbfile.close()   
        first_time = 0
    
        #pbar.update()
    print('[*] Successfully arriving target times: {} | Crash times: {}'.format(success_time, collision_time))
    outfile = open("navigation_result.txt", 'a')
    outfile.write(str(success_time)+' '+str(collision_time)+'\n')
    outfile.close()