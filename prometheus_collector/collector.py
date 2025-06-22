import argparse
import os
from prometheus_client import start_http_server, Gauge, Info
import telnetlib3
import asyncio
import time
import re

parser = argparse.ArgumentParser(description='GPON Metrics Collector')
parser.add_argument('--hostname', action='append',
                    help='Hostname or IP of the GPON device (env: GPON_HOSTNAMES, comma-separated)')
parser.add_argument('--port', action='append', type=int,
                    help='Telnet port of the GPON device (env: GPON_PORTS, comma-separated)')
parser.add_argument('--user', action='append',
                    help='Username for telnet authentication (env: GPON_USERS, comma-separated)')
parser.add_argument('--password', action='append',
                    help='Password for telnet authentication (env: GPON_PASSWORDS, comma-separated)')
parser.add_argument('--webserver-port', type=int,
                    default=int(os.getenv('GPON_WEBSERVER_PORT', '8111')),
                    help='Port for the Prometheus metrics web server to listen on (env: GPON_WEBSERVER_PORT)')
parser.add_argument('--fetch-interval', type=int,
                    default=int(os.getenv('GPON_FETCH_INTERVAL', '60')),
                    help='Interval (in seconds) between metric fetches (env: GPON_FETCH_INTERVAL)')

args = parser.parse_args()


# Handle environment variables for list arguments
def parse_env_list(env_var, arg_list, convert_func=str, default_value=None):
    if arg_list:
        return arg_list
    env_value = os.getenv(env_var)
    if env_value:
        return [convert_func(x.strip()) for x in env_value.split(',')]
    return [default_value] if default_value is not None else []


# Apply environment variable defaults
if not args.hostname:
    args.hostname = parse_env_list('GPON_HOSTNAMES', args.hostname)
if not args.port:
    args.port = parse_env_list('GPON_PORTS', args.port, int, 23)
if not args.user:
    args.user = parse_env_list('GPON_USERS', args.user)
if not args.password:
    args.password = parse_env_list('GPON_PASSWORDS', args.password)

# Validate required arguments
if not args.hostname:
    parser.error('--hostname is required (or set GPON_HOSTNAMES environment variable)')
if not args.port:
    parser.error('--port is required (or set GPON_PORTS environment variable)')
if not args.user:
    parser.error('--user is required (or set GPON_USERS environment variable)')
if not args.password:
    parser.error('--password is required (or set GPON_PASSWORDS environment variable)')

# Validate that all lists have the same length
list_lengths = [len(args.hostname), len(args.port), len(args.user), len(args.password)]
if len(set(list_lengths)) != 1:
    parser.error('All device configuration lists (hostname, port, user, password) must have the same length')

temperature_gauge = Gauge('gpon_temperature_celsius', 'Temperature of the GPON device in Celsius', ['ip'])
voltage_gauge = Gauge('gpon_voltage_volts', 'Voltage of the GPON device in Volts', ['ip'])
tx_power_gauge = Gauge('gpon_tx_power_dbm', 'Tx Power of the GPON device in dBm', ['ip'])
rx_power_gauge = Gauge('gpon_rx_power_dbm', 'Rx Power of the GPON device in dBm', ['ip'])
bias_current_gauge = Gauge('gpon_bias_current_mA', 'Bias Current of the GPON device in mA', ['ip'])
onu_state_gauge = Gauge('gpon_onu_state', 'ONU State of the GPON device', ['ip'])

onu_state_mapping = {
    '01': 1,
    '02': 2,
    '03': 3,
    '04': 4,
    'O5': 5,
    '06': 6,
    '07': 7,
}


async def wait_for_prompt(reader, timeout=60):
    """Wait for a command prompt (typically ending with $, #, or >)"""
    try:
        buffer = ""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=1)
                if not chunk:
                    break

                # Ensure chunk is a string
                if isinstance(chunk, bytes):
                    chunk = chunk.decode('utf-8', errors='ignore')

                buffer += chunk

                # Check if we have a prompt-like ending
                if buffer.endswith('$ ') or buffer.endswith('# ') or buffer.endswith('> ') or \
                        buffer.endswith('$') or buffer.endswith('#') or buffer.endswith('>'):
                    return buffer

            except asyncio.TimeoutError:
                continue

        return buffer

    except Exception as e:
        print(f"Error waiting for prompt: {e}")
        return ""


async def execute_telnet_command(reader, writer, command, timeout=30):
    """Execute a command via telnet and return the response"""
    try:
        writer.write(command + '\r\n')
        await writer.drain()

        # Wait for response
        response = await wait_for_prompt(reader, timeout)
        return response

    except Exception as e:
        print(f"Error executing command '{command}': {e}")
        return ""


async def fetch_and_update_metrics_via_telnet(hostname, port, username, password):
    """Connect via telnet and fetch metrics"""
    try:
        print(f"Connecting to {hostname}:{port}")

        # Connect to telnet server
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(hostname, port),
            timeout=15
        )

        # Wait for initial connection response
        initial_response = await wait_for_prompt(reader, timeout=10)
        print(f"Initial response from {hostname}: {initial_response[:100]}...")

        # Send username
        if 'login:' in initial_response.lower() or 'username:' in initial_response.lower():
            writer.write(username + '\r\n')
            await writer.drain()

            # Wait for password prompt
            password_response = await wait_for_prompt(reader, timeout=10)
            print(f"Password prompt from {hostname}: {password_response[:100]}...")

            if 'password:' in password_response.lower():
                writer.write(password + '\r\n')
                await writer.drain()

                # Wait for shell prompt
                shell_response = await wait_for_prompt(reader, timeout=10)
                print(f"Shell prompt from {hostname}: {shell_response[:100]}...")
            else:
                print(f"No password prompt received from {hostname}")
                writer.close()
                return
        else:
            print(f"No login prompt received from {hostname}")
            writer.close()
            return

        commands = {
            'diag pon get transceiver bias-current': bias_current_gauge,
            'diag pon get transceiver rx-power': rx_power_gauge,
            'diag pon get transceiver temperature': temperature_gauge,
            'diag pon get transceiver tx-power': tx_power_gauge,
            'diag pon get transceiver voltage': voltage_gauge,
            'diag gpon get onu-state': onu_state_gauge,
        }

        for command, gauge in commands.items():
            print(f"Executing command on {hostname}: {command}")
            result = await execute_telnet_command(reader, writer, command)
            print(f"Command result: {result[:200]}...")

            if command in ['diag pon get transceiver rx-power', 'diag pon get transceiver tx-power']:
                value = re.search(r'(-?\d+\.\d+)', result)
                if value:
                    gauge.labels(ip=hostname).set(float(value.group(0)))
                    print(f"Set {command} = {value.group(0)}")
            elif command.startswith('diag gpon get onu-state'):
                state_code = re.search(r'ONU state: (.*)', result)
                if state_code:
                    gauge.labels(ip=hostname).set(onu_state_mapping.get(state_code.group(1), 0))
                    print(f"Set ONU state = {state_code.group(1)}")
            else:
                value = re.search(r'(\d+\.\d+)', result)
                if value:
                    gauge.labels(ip=hostname).set(float(value.group(0)))
                    print(f"Set {command} = {value.group(0)}")

        # Close connection
        writer.close()
        await writer.wait_closed()
        print(f"Successfully collected metrics from {hostname}")

    except asyncio.TimeoutError:
        print(f"Timeout connecting to {hostname}:{port}")
    except Exception as e:
        print(f"Error connecting to {hostname}:{port} - {e}")


def fetch_and_update_metrics_via_telnet_sync(hostname, port, username, password):
    """Synchronous wrapper for the async telnet function"""
    try:
        asyncio.run(fetch_and_update_metrics_via_telnet(hostname, port, username, password))
    except Exception as e:
        print(f"Error in telnet sync wrapper for {hostname}: {e}")


def main():
    start_http_server(args.webserver_port)
    print(f"Started Prometheus metrics server on port {args.webserver_port}")
    print(f"Monitoring {len(args.hostname)} GPON devices")

    while True:
        for i in range(len(args.hostname)):
            print(f"Fetching metrics from {args.hostname[i]}:{args.port[i]}")
            fetch_and_update_metrics_via_telnet_sync(args.hostname[i], args.port[i], args.user[i], args.password[i])
        print(f"Waiting {args.fetch_interval} seconds before next collection...")
        time.sleep(args.fetch_interval)


if __name__ == "__main__":
    main()
