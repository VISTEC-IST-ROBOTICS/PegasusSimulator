#!/usr/bin/env python
import carb
import time
import numpy as np
from threading import Thread, Lock
from pymavlink import mavutil

class SensorSource:
    """
    The binary codes to signal which simulated data is being sent through mavlink
    """
    ACCEL: int = 7          # 0b111
    GYRO: int = 56          # 0b111000
    MAG: int = 448          # 0b111000000
    BARO: int = 6656        # 0b1101000000000
    DIFF_PRESS: int = 1024  # 0b10000000000

class SensorMsg:
    """
    Class that defines the sensor data that can be sent through mavlink
    """

    def __init__(self):

        # IMU Data
        self.new_imu_data: bool = False
        self.xacc: float = 0.0
        self.yacc: float = 0.0
        self.zacc: float = 0.0 
        self.xgyro: float = 0.0 
        self.ygyro: float = 0.0 
        self.zgyro: float = 0.0

        # Baro Data
        self.new_bar_data: bool = False
        self.abs_pressure: float = 0.0
        self.pressure_alt: float = 0.0
        self.temperature: float = 0.0

        # Magnetometer Data
        self.new_mag_data: bool = False
        self.xmag: float = 0.0
        self.ymag: float = 0.0
        self.zmag: float = 0.0

        # Airspeed Data
        self.new_press_data: bool = False
        self.diff_pressure: float = 0.0

        # GPS Data
        self.new_gps_data: bool = False
        self.fix_type: int = 0
        self.latitude_deg: float = -999
        self.longitude_deg: float = -999
        self.altitude: float = -999
        self.eph: float = 1.0
        self.epv: float = 1.0
        self.velocity: float = 0.0
        self.velocity_north: float = 0.0
        self.velocity_east: float = 0.0
        self.velocity_down: float = 0.0
        self.cog: float = 0.0
        self.satellites_visible: int = 0


class MavlinkInterface:

    def __init__(self, connection: str, num_thrusters:int = 4, enable_lockstep: bool = True):

        # Connect to the mavlink server
        self._connection_port = connection
        self._connection = mavutil.mavlink_connection(self._connection_port)
        self._update_rate: float = 250.0 # Hz
        self._is_running: bool = False

        # GPS constants
        self._GPS_fix_type: int = int(3)
        self._GPS_satellites_visible = int(10)
        self._GPS_eph: int = int(1)
        self._GPS_epv: int = int(1)

        # Vehicle Sensor data to send through mavlink
        self._sensor_data: SensorMsg = SensorMsg()

        # Vehicle actuator control data
        self._num_inputs: int = num_thrusters
        self._input_reference: np.ndarray = np.zeros((self._num_inputs,))
        self._armed: bool = False

        self._input_offset: np.ndarray = np.zeros((self._num_inputs,))
        self._input_scaling: np.ndarray = np.zeros((self._num_inputs,))

        # TODO - input_reference = (mavlink_input + input_offset) * input_scalling + zero_position_armed

        # Select whether lockstep is enabled
        self._enable_lockstep: bool = enable_lockstep

        # Auxiliar variables to handle the lockstep between receiving sensor data and actuator control
        self._received_first_imu: bool = False
        self._received_first_actuator: bool = False

        self._received_imu: bool = False        
        self._received_actuator: bool = False

        # Auxiliar variables to check if we have already received an hearbeat from the software in the loop simulation
        self._received_first_hearbeat: bool = False
        self._first_hearbeat_lock: Lock = Lock()

        self._last_heartbeat_sent_time = 0

    def update_imu_data(self, data):

        # TODO - check if we need to rotate this! Probably we do!

        # Acelerometer data
        self._sensor_data.xacc = data["linear_acceleration"][0]
        self._sensor_data.yacc = data["linear_acceleration"][1]
        self._sensor_data.zacc = data["linear_acceleration"][2]

        # Gyro data
        self._sensor_data.xgyro = data["angular_velocity"][0]
        self._sensor_data.ygyro = data["angular_velocity"][1]
        self._sensor_data.zgyro = data["angular_velocity"][2]

        # Signal that we have new IMU data
        self._sensor_data.new_imu_data = True

    def update_gps_data(self, data):
    
        # GPS data
        self._sensor_data.fix_type = int(data["fix_type"])
        self._sensor_data.latitude_deg = int(data["latitude"] * 10000000)
        self._sensor_data.longitude_deg = int(data["longitude"] * 10000000)
        self._sensor_data.altitude = int(data["altitude"] * 1000)
        self._sensor_data.eph = int(data["eph"])
        self._sensor_data.epv = int(data["epv"])
        self._sensor_data.velocity = int(data["speed"] * 100)
        self._sensor_data.velocity_north = int(data["velocity_north"] * 100)
        self._sensor_data.velocity_east = int(data["velocity_east"] * 100)
        self._sensor_data.velocity_down = int(data["velocity_down"] * 100)
        self._sensor_data.cog = int(data["cog"] * 100)
        self._sensor_data.satellites_visible = int(data["sattelites_visible"])

        # Signal that we have new GPS data
        self._sensor_data.new_gps_data = True

    def update_bar_data(self, data):
        
        # Barometer data
        self._sensor_data.temperature = data["temperature"]
        self._sensor_data.abs_pressure = data["absolute_pressure"]
        self._sensor_data.pressure_alt = data["pressure_altitude"]

        # Signal that we have new Barometer data
        self._sensor_data.new_bar_data = True

    def update_mag_data(self, data):

        # Magnetometer data
        self._sensor_data.xmag = data["magnetic_field"][0]
        self._sensor_data.ymag = data["magnetic_field"][1]
        self._sensor_data.zmag = data["magnetic_field"][2]  

        # Signal that we have new Magnetometer data
        self._sensor_data.new_mag_data = True

    def __del__(self):

        # When this object gets destroyed, close the mavlink connection to free the communication port
        if self._connection is not None:
            self._connection.close()

    def start_stream(self):
        
        # If we are already running the mavlink interface, then ignore the function call
        if self._is_running == True:
            return

        # If the connection no longer exists (we stoped and re-started the stream, then re_intialize the interface)
        if self._connection is None:
            self.re_initialize_interface()
        
        # Set the flag to signal that the mavlink transmission has started
        self._is_running = True

        # Create a thread for polling for mavlink messages (but only start after receiving the first hearbeat)
        self._update_thread: Thread = Thread(target=self.mavlink_update)

        # Start a new thread that will wait for a new hearbeat
        Thread(target=self.wait_for_first_hearbeat).start()

    def stop_stream(self):
        
        # If the simulation was already stoped, then ignore the function call
        if self._is_running == False:
            return

        # Set the flag so that we are no longer running the mavlink interface
        self._is_running = False

        # Terminate the infinite thread loop
        self._update_thread.join()

        # Close the mavlink connection
        self._connection.close()
        self._connection = None

    def re_initialize_interface(self):

        self._is_running: bool = False
        
        # Restart the connection
        self._connection = mavutil.mavlink_connection(self._connection_port)

        # Auxiliar variables to handle the lockstep between receiving sensor data and actuator control
        self._received_first_imu: bool = False
        self._received_first_actuator: bool = False

        self._received_imu: bool = False        
        self._received_actuator: bool = False

        # Auxiliar variables to check if we have already received an hearbeat from the software in the loop simulation
        self._received_first_hearbeat: bool = False
        self._first_hearbeat_lock: Lock = Lock()

        self._last_heartbeat_sent_time = 0

    def update_imu(self, imu_data):
        self._received_imu = True

    def wait_for_first_hearbeat(self):
        """
        Method that is responsible for waiting for the first hearbeat. This method is locking and will only return
        if an hearbeat is received via mavlink. When this first heartbeat is received, a new thread will be created to
        poll for mavlink messages
        """

        carb.log_warn("Waiting for first hearbeat")
        self._connection.wait_heartbeat()

        # Set the first hearbeat flag to true
        with self._first_hearbeat_lock:
            self._received_first_hearbeat = True
        
        carb.log_warn("Received first hearbeat")

        # Start updating mavlink messages at a fixed rate
        self._update_thread.start()

    def mavlink_update(self):
        """
        Method that is running in a thread in parallel to send the mavlink data 
        """

        # Run this thread forever at a fixed rate
        while self._is_running:
            
            # Check if we have already received IMU data. If so, we start the lockstep and wait for more imu data
            if self._received_first_imu:
                while not self._received_imu and self._is_running:
                    # TODO - here
                    pass
            
            self._received_imu = False        

            # Check if we have received any mavlink messages
            self.poll_mavlink_messages()

            # Send hearbeats at 1Hz
            if (time.time() - self._last_heartbeat_sent_time) > 1.0 or self._received_first_hearbeat == False:
                self.send_heartbeat()
                self._last_heartbeat_sent_time = time.time()

            # Send sensor messages
            self.send_sensor_msgs()            

            # Send groundtruth
            self.send_ground_truth()

            # TODO - handle mavlink disconnections from the SITL here (TO BE DONE)

            # Handle the control input to the motors
            self.handle_control()
            
            # Update at 250Hz
            time.sleep(1.0/self._update_rate)
        

    def poll_mavlink_messages(self):
        """
        Method that is used to check if new mavlink messages were received
        """

        # If we have not received the first hearbeat yet, do not poll for mavlink messages
        with self._first_hearbeat_lock:
            if self._received_first_hearbeat == False:
                return

        # Check if we need to lock and wait for actuator control data
        needs_to_wait_for_actuator: bool = self._received_first_actuator and self._enable_lockstep

        # Start by assuming that we have not received data for the actuators for the current step
        self._received_actuator = False

        # Use this loop to emulate a do-while loop (make sure this runs at least once)
        while True:
            
            # Try to get a message
            msg = self._connection.recv_match(blocking=needs_to_wait_for_actuator)
            carb.log_warn(msg)

            #if msg is not None:
            #    pass

                # TODO - check if message is of the type actuator, and if so, then:
                # self._received_first_actuator = True
                # self._received_actuator = True

            # Check if we do not need to wait for an actuator message or we just received actuator input
            # If so, break out of the infinite loop
            if not needs_to_wait_for_actuator or self._received_actuator:
                break

    def send_heartbeat(self, mav_type=mavutil.mavlink.MAV_TYPE_GENERIC):
        """
        Method that is used to publish an heartbear through mavlink protocol
        """

        carb.log_warn("Sending heartbeat")

        # Note: to know more about these functions, go to pymavlink->dialects->v20->standard.py
        # This contains the definitions for sending the hearbeat and simulated sensor messages
        self._connection.mav.heartbeat_send(
            mav_type,
            mavutil.mavlink.MAV_AUTOPILOT_INVALID,
            0, 0, 0)

    def send_sensor_msgs(self):
        """
        Method that when invoked, will send the simulated sensor data through mavlink
        """
        carb.log_warn("Sending sensor msgs")

        # Check which sensors have new data to send
        fields_updated: int = 0

        if self._sensor_data.new_imu_data:
            # Set the bit field to signal that we are sending updated accelerometer and gyro data
            fields_updated = fields_updated | SensorSource.ACCEL | SensorSource.GYRO
            self._sensor_data.new_imu_data = False

        if self._sensor_data.new_mag_data:
            # Set the bit field to signal that we are sending updated magnetometer data
            fields_updated = fields_updated | SensorSource.MAG
            self._sensor_data.new_mag_data = False

        if self._sensor_data.new_bar_data:
            # Set the bit field to signal that we are sending updated barometer data
            fields_updated = fields_updated | SensorSource.BARO
            self._sensor_data.new_bar_data = False

        if self._sensor_data.new_press_data:
            # Set the bit field to signal that we are sending updated diff pressure data
            fields_updated = fields_updated | SensorSource.DIFF_PRESS
            self._sensor_data.new_press_data = False

        try:
            self._connection.mav.hil_sensor_send(
                int(time.time()),
                self._sensor_data.xacc,
                self._sensor_data.yacc,
                self._sensor_data.zacc,
                self._sensor_data.xgyro,
                self._sensor_data.ygyro,
                self._sensor_data.zgyro,
                self._sensor_data.xmag,
                self._sensor_data.ymag,
                self._sensor_data.zmag,
                self._sensor_data.abs_pressure,
                self._sensor_data.diff_pressure,
                self._sensor_data.pressure_alt,
                self._sensor_data.altitude,
                fields_updated
            )
        except:
            carb.log_warn("Could not send sensor data through mavlink")

    def send_gps_msgs(self):
        """
        Method that is used to send simulated GPS data through the mavlink protocol. Receives as argument
        a dictionary with the simulated gps data
        """
        carb.log_warn("Sending GPS msgs")

        # Do not send GPS data, if no new data was received
        if not self._sensor_data.new_gps_data:
            return
        
        self._sensor_data.new_gps_data = False
        
        # Latitude, longitude and altitude (all in integers)
        try:
            self._connection.mav.hil_gps_send(
                int(time.time()),
                self._sensor_data.fix_type,
                self._sensor_data.latitude_deg,
                self._sensor_data.longitude_deg,
                self._sensor_data.altitude,
                self._sensor_data.eph,
                self._sensor_data.epv,
                self._sensor_data.velocity,
                self._sensor_data.velocity_north,
                self._sensor_data.velocity_east,
                self._sensor_data.velocity_down,
                self._sensor_data.cog,
                self._sensor_data.satellites_visible
            )
        except:
            carb.log_warn("Could not send gps data through mavlink")

    def send_ground_truth(self):
        # TODO
        pass

    def handle_control(self):
        #TODO
        pass