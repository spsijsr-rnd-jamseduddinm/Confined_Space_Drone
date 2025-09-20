# app.py
import os
import sys
import subprocess
import threading
import time
import signal
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import select
import psutil
import queue
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", ping_timeout=5, ping_interval=2, logger=True, engineio_logger=True)

# Store terminal processes and their output
terminals = {}
terminal_outputs = {i: [] for i in range(1, 7)}
output_queues = {i: queue.Queue() for i in range(1, 7)}

# ROS environment setup - this is critical for ROS commands to work
ros_setup_commands = [
    'source /opt/ros/noetic/setup.bash',
    'source /home/nvidia/dlio_ws/devel/setup.bash',
    'export PYTHONPATH=/opt/ros/noetic/lib/python3/dist-packages:$PYTHONPATH',
    'export ROS_MASTER_URI=http://localhost:11311',
    'export ROS_HOSTNAME=localhost'
]

terminal_commands = {
    1: {
        'name': 'DLIO',
        'init': ros_setup_commands,
        'start': 'roslaunch direct_lidar_inertial_odometry dlio.launch rviz:=false pointcloud_topic:=/rslidar_points imu_topic:=/rslidar_imu_data',
        'delay': 2
    },
    2: {
        'name': 'Mavros to Pixhawk',
        'init': ros_setup_commands,
        'start': 'roslaunch mavros apm.launch fcu_url:=/dev/ttyUSB0:921600',
        'delay': 3
    },
    3: {
        'name': 'Airy SDK',
        'init': ros_setup_commands,
        'start': 'roslaunch rslidar_sdk start.launch',
        'delay': 4
    },
    4: {
        'name': 'Save DLIO Output',
        'init': ros_setup_commands,
        'start': 'python3 /home/nvidia/dlio_ws/src/saverostopic.py',
        'delay': 5
    },
    5: {
        'name': 'DLIO to Mavros Publisher',
        'init': ros_setup_commands,
        'start': 'python3 /home/nvidia/dlio_ws/src/dlio-Mavros_bridge/scripts/test13.py',
        'delay': 1
    },
    6: {
        'name': 'Save PCAP File',
        'start': 'tshark -i eth0 -w /home/nvidia/FLASK_CSD/PCAP_tshark/capture.pcap',
        'delay': 1
    }
}

# Track if we've auto-started the terminals
auto_started = False

# Function to create a shell command with proper environment setup
def create_shell_command(init_commands, main_command):
    if init_commands:
        # Combine all initialization commands with the main command
        return f"{' && '.join(init_commands)} && {main_command}"
    else:
        return main_command

# Function to read output from a process
def read_process_output(process, terminal_id):
    while True:
        try:
            if process.poll() is not None:
                break
                
            # Use non-blocking read
            try:
                output = process.stdout.readline()
                if output:
                    output_text = output.decode('utf-8', errors='ignore').strip()
                    if output_text:
                        # Store the output
                        terminal_outputs[terminal_id].append(output_text)
                        # Keep only last 100 lines to prevent memory issues
                        if len(terminal_outputs[terminal_id]) > 100:
                            terminal_outputs[terminal_id] = terminal_outputs[terminal_id][-100:]
                        
                        # Send update to all connected clients via SocketIO
                        socketio.emit('terminal_output', {
                            'id': terminal_id,
                            'output': output_text + '\n',
                            'timestamp': datetime.now().isoformat()
                        })
            except Exception as e:
                # Non-blocking read might raise an exception when no data is available
                time.sleep(0.1)
                
        except Exception as e:
            logger.error(f"Error reading output from terminal {terminal_id}: {e}")
            break

# Function to start a specific terminal process
def start_terminal(id):
    # Stop if already running
    if id in terminals and terminals[id].get('process') and terminals[id]['process'].poll() is None:
        stop_terminal(id)
        time.sleep(1)
    
    try:
        # Get the command info
        cmd_info = terminal_commands[id]
        
        # Create the full command with environment setup
        if 'init' in cmd_info:
            full_cmd = create_shell_command(cmd_info['init'], cmd_info['start'])
        else:
            full_cmd = cmd_info['start']
        
        logger.debug(f"Starting terminal {id} with command: {full_cmd}")
        
        # Create a process with the start command
        process = subprocess.Popen(
            ['bash', '-c', full_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            preexec_fn=os.setsid,  # Create new process group for proper signal handling
            bufsize=1,  # Line buffered
            universal_newlines=True  # Text mode
        )
        
        # Store the process
        terminals[id] = {
            'process': process,
            'running': True,
            'start_time': datetime.now()
        }
        
        # Start a thread to read the output
        threading.Thread(target=read_process_output, args=(process, id), daemon=True).start()
        
        return True
    except Exception as e:
        logger.error(f"Error starting terminal {id}: {e}")
        # Add error to output
        error_msg = f"Error starting terminal: {str(e)}"
        terminal_outputs[id].append(error_msg)
        socketio.emit('terminal_output', {
            'id': id,
            'output': error_msg + '\n',
            'timestamp': datetime.now().isoformat()
        })
        return False

# Function to stop a terminal process
def stop_terminal(id):
    if id in terminals and terminals[id].get('process'):
        try:
            process = terminals[id]['process']
            
            # Try to send Ctrl+C signal to the process group
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except ProcessLookupError:
                pass  # Process already terminated
            
            # Wait a bit for graceful termination
            time.sleep(1)
            
            # Force kill if still running
            if process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            
            terminals[id]['running'] = False
            return True
        except Exception as e:
            logger.error(f"Error stopping terminal {id}: {e}")
            return False
    return False

# Function to automatically start all terminals
def auto_start_terminals():
    global auto_started
    if not auto_started:
        logger.info("Auto-starting all terminals...")
        results = {}
        for id in range(1, 7):
            success = start_terminal(id)
            results[id] = 'started' if success else 'failed'
            time.sleep(terminal_commands[id].get('delay', 1))
        auto_started = True
        logger.info(f"Auto-start completed: {results}")

# Initialize all terminals when the app starts
for i in range(1, 7):
    terminals[i] = {'process': None, 'running': False}

# Start a thread to auto-start terminals after a short delay
def delayed_auto_start():
    time.sleep(3)  # Wait for server to fully start
    auto_start_terminals()

auto_start_thread = threading.Thread(target=delayed_auto_start, daemon=True)
auto_start_thread.start()

# Route for the main page
@app.route('/')
def index():
    return render_template('index02.html', terminals=terminal_commands)

# API endpoint to start a specific terminal
@app.route('/start_terminal/<int:id>', methods=['POST'])
def api_start_terminal(id):
    if 1 <= id <= 6:
        success = start_terminal(id)
        if success:
            return jsonify({'status': 'started', 'id': id})
        else:
            return jsonify({'status': 'error', 'message': f'Failed to start terminal {id}'})
    return jsonify({'status': 'error', 'message': 'Invalid terminal ID'})

# API endpoint to stop a specific terminal
@app.route('/stop_terminal/<int:id>', methods=['POST'])
def api_stop_terminal(id):
    if 1 <= id <= 6:
        success = stop_terminal(id)
        if success:
            return jsonify({'status': 'stopped', 'id': id})
        else:
            return jsonify({'status': 'error', 'message': f'Failed to stop terminal {id}'})
    return jsonify({'status': 'error', 'message': 'Invalid terminal ID'})

# API endpoint to get terminal output
@app.route('/terminal_output/<int:id>')
def get_terminal_output(id):
    if 1 <= id <= 6:
        # Get last 5 lines of output
        lines = terminal_outputs[id]
        last_five = '\n'.join(lines[-5:]) if len(lines) > 5 else '\n'.join(lines)
        return jsonify({'output': last_five})
    return jsonify({'output': ''})

# API endpoint to start all terminals
@app.route('/start_all', methods=['POST'])
def start_all():
    results = {}
    for id in range(1, 7):
        success = start_terminal(id)
        results[id] = 'started' if success else 'failed'
        time.sleep(terminal_commands[id].get('delay', 1))
    return jsonify({'status': 'completed', 'results': results})

# API endpoint to stop all terminals
@app.route('/stop_all', methods=['POST'])
def stop_all():
    results = {}
    for id in range(1, 7):
        success = stop_terminal(id)
        results[id] = 'stopped' if success else 'failed'
    return jsonify({'status': 'completed', 'results': results})

# API endpoint to get terminal status
@app.route('/terminal_status/<int:id>')
def terminal_status(id):
    if 1 <= id <= 6:
        if id in terminals and terminals[id].get('process'):
            is_running = terminals[id]['process'].poll() is None
            return jsonify({'id': id, 'running': is_running})
    return jsonify({'id': id, 'running': False})

# SocketIO event for connection
@socketio.on('connect')
def handle_connect():
    logger.debug('Client connected')
    emit('connection_status', {'status': 'connected', 'timestamp': datetime.now().isoformat()})
    
    # Send initial terminal outputs to the newly connected client
    for id in range(1, 7):
        lines = terminal_outputs[id]
        last_five = '\n'.join(lines[-5:]) if len(lines) > 5 else '\n'.join(lines)
        emit('terminal_output', {
            'id': id,
            'output': last_five + '\n' if last_five else '',
            'timestamp': datetime.now().isoformat()
        })

# SocketIO event for disconnection
@socketio.on('disconnect')
def handle_disconnect():
    logger.debug('Client disconnected')

# Ping event for connection monitoring
@socketio.on('ping')
def handle_ping():
    emit('pong', {'timestamp': datetime.now().isoformat()})

# Health check endpoint
@app.route('/health')
def health_check():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

if __name__ == '__main__':
    logger.info("Starting Terminal Manager application...")
    socketio.run(app, host='0.0.0.0', port=5005, debug=False, allow_unsafe_werkzeug=False)