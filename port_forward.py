#!/usr/bin/env python
"""request a port forwarding so that serve-port is reachable on public-port

  either '--yaml-from-stdin' with yaml 'serve_port: <port>' from STDIN
  or     '--serve-port <port>' must be set

  '--yaml-to-stdout' outputs resulting configuration yaml to STDOUT,
    merged from STDIN yaml if '--yaml-from-stdin'

  can be used in combination with serve_once.py, eg.:
    r="$(printf 'serve_port: 48443\\nrequest_method: POST\\npayload: true\\nrequest_body_stdout: true\\n' \\
        | port_forward.py --yaml-from-stdin --yaml-to-stdout | serve_once.py --yes)"
"""


import argparse
import copy
import os
import socket
import sys
import textwrap

import natpmp
import yaml


def error_print(message, print_help=False):
    print("Error:  {}".format(message), file=sys.stderr)
    if print_help:
        print("        use -h, --help for a complete list of arguments")
    sys.exit(1)


def merge_dict_struct(struct1, struct2):
    "recursive merge of two dict like structs into one, struct2 takes precedence over struct1"

    def is_dict_like(v):
        return hasattr(v, "keys") and hasattr(v, "values") and hasattr(v, "items")

    def is_list_like(v):
        return hasattr(v, "append") and hasattr(v, "extend") and hasattr(v, "pop")

    merged = copy.deepcopy(struct1)
    if is_dict_like(struct1) and is_dict_like(struct2):
        for key in struct2:
            if key in struct1:
                # if the key is present in both dictionaries, recursively merge the values
                merged[key] = merge_dict_struct(struct1[key], struct2[key])
            else:
                merged[key] = struct2[key]
    elif is_list_like(struct1) and is_list_like(struct2):
        for item in struct2:
            if item not in struct1:
                merged.append(item)
    else:
        # if neither input is a dictionary or list, the second input overwrites the first input
        merged = struct2
    return merged


def get_default_gateway_ip():
    if os.path.exists("/proc/net/route"):
        r_data = open("/proc/net/route", "r").read()
        for r_line in r_data.splitlines():
            r_fields = r_line.strip().split("\t")
            if len(r_fields) >= 3 and r_fields[1] == "00000000":
                ip_hex = r_fields[2]
                if sys.byteorder == "little":
                    ip_bytes = bytes.fromhex(ip_hex)[::-1]
                else:
                    ip_bytes = bytes.fromhex(ip_hex)
                ip_address = socket.inet_ntoa(ip_bytes)
                return ip_address
    return None


def get_default_host_ip():
    try:
        gateway_addr = socket.gethostbyname(socket.gethostname())
        if (
            not socket.inet_pton(socket.AF_INET, gateway_addr)
            or gateway_addr.startswith("127.")
            or gateway_addr.startswith("::1")
        ):
            gateway_addr = None
    except socket.gaierror:
        gateway_addr = None
    return gateway_addr


def get_public_ip(config):
    gateway_ip, protocol, retry = (
        config["port_forward"][c] for c in ["gateway_ip", "protocol", "retry"]
    )
    if protocol == "natpmp":
        request = natpmp.PublicAddressRequest()
        response = natpmp.send_request_with_retry(
            gateway_ip=gateway_ip,
            request=request,
            response_data_class=natpmp.PublicAddressResponse,
            retry=retry,
            response_size=12,
        )
        if response.result == 0:
            return response.ip
        else:
            print(natpmp.error_str(response.result), file=sys.stderr)
            return None


def port_forward(config):
    serve_port = config["serve_port"]
    public_port, gateway_ip, protocol, lifetime, retry = (
        config["port_forward"][c]
        for c in ["public_port", "gateway_ip", "protocol", "lifetime", "retry"]
    )

    if protocol == "natpmp":
        request = natpmp.PortMapRequest(
            protocol=natpmp.NATPMP_PROTOCOL_TCP,
            private_port=serve_port,
            public_port=public_port,
            lifetime=lifetime,
        )
        response = natpmp.send_request_with_retry(
            gateway_ip=gateway_ip,
            request=request,
            response_data_class=natpmp.PortMapResponse,
            retry=retry,
        )
        if response.result == 0:
            return response.public_port
        else:
            print(natpmp.error_str(response.result), file=sys.stderr)
            return None


default_config_str = """
serve_port:
port_forward:
  protocol: "natpmp"
  gateway_ip:
  public_ip:
  public_port:
  lifetime: 3600
  retry: 9
"""

default_config = yaml.safe_load(default_config_str)
default_short = textwrap.fill(
    ", ".join(["{}: {}".format(k, v) for k, v in default_config.items()]),
    width=80,
    initial_indent="  ",
    subsequent_indent="  ",
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__ + "\ndefaults:\n{}\n".format(default_short),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--yaml-from-stdin", action="store_true", help="Read input from STDIN")
    parser.add_argument("--serve-port", type=int, help="internal port to be forwarded to")
    parser.add_argument(
        "--public-port",
        type=int,
        help="public port of packets incoming, will be set to serve-port if unset",
    )
    parser.add_argument(
        "--gateway-ip", type=str, help="gateway IP, will be inferred from network if unset"
    )
    parser.add_argument(
        "--protocol",
        type=str,
        default="natpmp",
        choices=["natpmp"],
        help="port forwarding protocol",
    )
    parser.add_argument("--lifetime", type=int, help="lifetime in seconds")
    parser.add_argument(
        "--yaml-to-stdout",
        action="store_true",
        help="print resulting config YAML to STDOUT, include merged YAML from STDIN",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="dont print anything to STDOUT, just exit 0 on success",
    )
    get_group = parser.add_argument_group("other Functions").add_mutually_exclusive_group()
    get_group.add_argument(
        "--get-host-ip", action="store_true", help="print default route host IP"
    )
    get_group.add_argument(
        "--get-gateway-ip", action="store_true", help="print default gateway IP"
    )
    get_group.add_argument(
        "--get-public-ip",
        action="store_true",
        help="request the public IP from gateway and print",
    )
    args = parser.parse_args()
    loaded_config = {"port_forward": {}}

    if not any(
        [
            args.yaml_from_stdin,
            args.serve_port,
            args.get_public_ip,
            args.get_gateway_ip,
            args.get_host_ip,
        ]
    ):
        error_print(
            "Need one of '--yaml-from-stdin', '--serve-port', '--get-public-ip', '--get-gateway-ip', '--get-host-ip'",
            print_help=True,
        )

    if args.get_host_ip:
        host_ip = get_default_host_ip()
        print(host_ip)
        sys.exit(0) if host_ip else sys.exit(1)

    if args.get_gateway_ip:
        gateway_ip = get_default_gateway_ip()
        print(gateway_ip)
        sys.exit(0) if gateway_ip else sys.exit(1)

    if args.yaml_from_stdin:
        stdin_str = sys.stdin.read()
        if not stdin_str.strip():
            error_print("Error: Arg --yaml-from-stdin supplied, but no data from STDIN")
        loaded_config = yaml.safe_load(stdin_str)
        if "serve_port" not in loaded_config:
            error_print("serve_port: <port> must be part of STDIN if --yaml-from-stdin")
        if "port_forward" not in loaded_config:
            loaded_config["port_forward"] = {}

    if args.serve_port:
        loaded_config["serve_port"] = args.serve_port

    for i in ["public_port", "gateway_ip", "lifetime", "retry"]:
        if hasattr(args, i):
            loaded_config["port_forward"][i] = getattr(args, i)

    # merge YAML config with defaults
    config = merge_dict_struct(default_config, loaded_config)

    # if still missing, fill in public_port and gateway_ip
    if not config["port_forward"]["public_port"]:
        config["port_forward"]["public_port"] = config["serve_port"]
    if not config["port_forward"]["gateway_ip"]:
        config["port_forward"]["gateway_ip"] = get_default_gateway_ip()

    public_ip = get_public_ip(config)
    if not public_ip:
        sys.exit(1)

    if args.get_public_ip:
        print(public_ip)
        sys.exit(0)

    public_port = port_forward(config)
    if not public_port:
        sys.exit(1)

    config["port_forward"]["public_ip"] = public_ip
    config["port_forward"]["public_port"] = public_port

    # print updated config to STDOUT if --yaml-to-stdout
    if args.yaml_to_stdout:
        print(yaml.safe_dump(config))
    elif not args.silent:
        print("{}:{}".format(public_ip, public_port))
