#!/usr/bin/python3

import os
import re
import sys
import json
import time
import requests
import traceback
import subprocess
from pathlib import Path
from threading import Thread
from datetime import datetime, timedelta

# XXX TODO - Add mutex around data shared by main and "ipfs add" threads
# XXX TODO - Forks

class IpfsExplorer():
    def __init__(self):
        # Node Credentials
        user = 'grin'
        with open(str(Path.home()) + '/.grin/main/.foreign_api_secret','r') as f:
            pas = f.read()
        self.node_auth = requests.auth.HTTPBasicAuth(user, pas)
        # Polling Schedule
        self.loop_delay = 10
        # Node API url
        self.grin_url="http://127.0.0.1:3413/v2/foreign"
        # Location to store block data
        self.block_data_dir = "/data/ipfs_explorer/blocks"
        # Dont change this
        self.group_size = 10000   # Number of files per directory
        #---
        self.running_ipfs_add = False

    # Get the height of chain tip from grin node
    def get_chain_height(self):
        payload = {
                "method": "get_tip",
                "params": [],
                "jsonrpc": "2.0",
                "id": 1
            }
        try:
            res = requests.post(
                    self.grin_url,
                    json = payload,
                    auth = self.node_auth,
                    timeout = 20,
                )
            chain_height = int(json.dumps(res.json()["result"]["Ok"]["height"]))
        except Exception as e:
            print("Faied to get_chain_height(): {}".format(e))
            return None
        return chain_height

    # Calculate data directory for a given block heaigh
    def height_to_dirname(self, height):
        subdir = int(height / self.group_size)
        return("{}/{}".format(self.block_data_dir, subdir))

    # Write block data to file
    def write_block_data(self, data, fork=0):
        height = data["header"]["height"]
        dname = self.height_to_dirname(height)
        if not os.path.exists(dname):
            os.makedirs(dname)
        fname = "{}/{}".format(dname, height)
        if fork:
            fname = fname + "_fork{}".format(fork)
        f = open(fname, "w")
        f.write(json.dumps(data))
        f.close()
    
    # Read block data
    # XXX TODO: remove fork argument, return all fork versions in an array ?
    def read_block_data(self, height, fork=0):
        dname = self.height_to_dirname(height)
        fname = "{}/{}".format(dname, height)
        if fork != 0:
            fname = fname + "_fork{}".format(fork)
        if not os.path.exists(fname):
            return None
        f = open(fname, "r")
        x = f.readline()
        f.close()
        j = json.loads(x)
        if int(j["header"]["height"]) != height:
            return None
        return j

    # Compare if 2 blocks are the same
    def same_block_data(self, a, b):
        if a is None or b is None:
            return False
        return a["header"]["hash"] == b["header"]["hash"]

    # Get a block from the grin node and write it to a file
    def get_and_process_block(self, height):
        payload = {"jsonrpc": "2.0", "method": "get_block", "params": [height, None, None], "id": 1}
        try:
            x=requests.post(self.grin_url, auth=self.node_auth, json=payload, timeout=20)
            block = x.json()["result"]["Ok"]
        except Exception as e:
            print("Error: {}".format(e))
            raise
        existing = self.read_block_data(height)
        if existing is None:
            self.write_block_data(block, False)
            print("Wrote Block: {}".format(block["header"]["height"]))
        # XXX TODO:  support more fork versions
        elif not self.same_block_data(block, existing):
            existing_fork = self.read_block_data(height, fork=1)
            if not self.same_block_data(block, existing_fork):
                self.write_block_data(block, fork=1)
                print("Wrote Block as fork: {}".format(block["header"]["height"]))
        else:
            return False # Nothing added
        return True # Added block data

    # Track the blockchain height
    def update_height(self, height):
        # Skip this if we are in the middle of an ipfs add
        if self.running_ipfs_add:
            return
        h_file = self.block_data_dir + "/height"
        height = int(height)
        try:
            f = open(h_file, "r")
            j = f.read()
            h = int(json.loads(j)["height"])
            f.close()
            if height <= h:
                return
        except Exception as e:
            print("Failed to grok existing height file: {}".format(e))
        j = { "height": height }
        f = open(h_file, "w")
        f.write(json.dumps(j))
        f.close()

    def ipfs_add_and_publish(self):
        print("ipfs_add_and_publish()")
        self.running_ipfs_add = True
        cmd = ["timeout", "300m", "/data/ipfs_explorer/bin/ipfs", "add", "-r", self.block_data_dir]
        try:
            message = subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=False).decode('utf-8')
            m = re.findall(r"added\s+(.*)\s+blocks[\r\n]", message)
            if len(m) != 1:
                print("Failed to get hash for ipfs add -r blocks: {}".format(message))
                return
            print("add hash = {}".format(m[0]))
        except Exception as e:
            print("Error ipfs add -r data: {}".format(e))
            return
        finally:
            self.running_ipfs_add = False
        cmd = ["timeout", "300m", "/data/ipfs_explorer/bin/ipfs", "name", "publish", m[0]]
        try:
            message = subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=False).decode('utf-8')
            print("Result of ipfs publish: {}".format(message))
        except Exception as e:
            print("Error ipfs publish: {}".format(e))
            return

    def run(self):
        # Disable stdout line buffering so log messages are printed in realtime
        sys.stdout.reconfigure(line_buffering=True)
        print("Starting Grin IPFS Explorer Service")

        # Block Depths for scan schedules
        WEEK_BLOCKS = 60*24*7
        DAY_BLOCKS = 60*24
        HOUR_BLOCKS = 60
    
        # Test node API connection
        chain_height = self.get_chain_height()
        if chain_height is None:
            print("Failed to connect to grin node foreign api.  Can not continue")
            sys.exit(1)

        # Initialize scheduled scan status structures
        full_scan_st = {"height":chain_height, "date":datetime.now()}
        week_scan_st = {"height":chain_height, "date":datetime.now()}
        day_scan_st = {"height":chain_height-DAY_BLOCKS, "date":datetime(1970, 1, 1)}
        hour_scan_st = {"height":chain_height-HOUR_BLOCKS, "date":datetime(1970, 1, 1)}
        min_scan_st = {"height":chain_height-1, "date":datetime(1970, 1, 1)}
        publish_st = {"dirty":False, "date":datetime(1970, 1, 1)}
    
        while True:
            # Init loop variables
            any_running = False  # Any active scheduled scan?
            any_new = False      # Any new data written in this time around the loop?
        
            # Get current chain height
            chain_height = self.get_chain_height()
            if chain_height is None:
                print("Unable to contact node foreign api, will try again...")
                time.sleep(self.loop_delay)

            try:
                # Scan Full blockchain
                if datetime.now() - full_scan_st["date"] > timedelta(weeks=2):
                    any_running = True
                    any_new = any_new or self.get_and_process_block(full_scan_st["height"])
                    if chain_height <= full_scan_st["height"] + 1:
                        full_scan_st["height"] = 0
                        full_scan_st["date"] = datetime.now()
                    else:
                        full_scan_st["height"] = full_scan_st["height"] + 1

                # Scan blocks from the past Week
                if datetime.now() - week_scan_st["date"] > timedelta(days=3):
                    any_running = True
                    any_new = any_new or self.get_and_process_block(week_scan_st["height"])
                    if chain_height <= week_scan_st["height"] + 1:
                        week_scan_st["height"] = chain_height - WEEK_BLOCKS
                        week_scan_st["date"] = datetime.now()
                    else:
                        week_scan_st["height"] = week_scan_st["height"] + 1

                # Scan blocks from the past Day 
                if datetime.now() - day_scan_st["date"] > timedelta(hours=8):
                    any_running = True
                    any_new = any_new or self.get_and_process_block(day_scan_st["height"])
                    if chain_height <= day_scan_st["height"] + 1:
                        day_scan_st["height"] = chain_height - DAY_BLOCKS
                        day_scan_st["date"] = datetime.now()
                    else:
                        day_scan_st["height"] = day_scan_st["height"] + 1

                # Scan blocks from the last hour
                if datetime.now() - hour_scan_st["date"] > timedelta(hours=2):
                    any_running = True
                    any_new = any_new or self.get_and_process_block(hour_scan_st["height"])
                    if chain_height <= hour_scan_st["height"] + 1:
                        hour_scan_st["height"] = chain_height - HOUR_BLOCKS
                        hour_scan_st["date"] = datetime.now()
                    else:
                        hour_scan_st["height"] = hour_scan_st["height"] + 1

                # Scan for new blocks continuously
                if datetime.now() - min_scan_st["date"] > timedelta(seconds=30):
                    if min_scan_st["height"] <= chain_height:
                        any_running = True
                        any_new = any_new or self.get_and_process_block(min_scan_st["height"])
                        min_scan_st["date"] = datetime.now()
                        min_scan_st["height"] = min_scan_st["height"] + 1
        
                # Accounting
                if any_new:
                    self.update_height(min_scan_st["height"]-1)
                    publish_st["dirty"] = True
        
                # Launch a thread to Add new block data to IPFS and publish to IPNS
                if not any_running:
                    if publish_st["dirty"] and datetime.now() - publish_st["date"] > timedelta(hours=6):
                        print("add and publish to ipfs")
                        ipfs_add_thread = Thread(target = self.ipfs_add_and_publish)
                        ipfs_add_thread.daemon = True
                        ipfs_add_thread.start()
                        publish_st["dirty"] = False
                        publish_st["date"] = datetime.now()
        
                # Throttle polling for new blocks if no scan is actively running
                if not any_running:
                    time.sleep(self.loop_delay)
            except Exception as e:
                print("Error: {} - {}".format(e, traceback.format_exc()))


if __name__ == "__main__":
    explorer = IpfsExplorer()
    explorer.run()
