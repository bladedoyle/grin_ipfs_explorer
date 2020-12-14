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

    # Calculate data directory for a given block height
    def height_to_dirname(self, height):
        subdir = int(height / self.group_size)
        return("{}/{}".format(self.block_data_dir, subdir))

    # Write block data to file - possibly as a fork
    def write_block_data(self, data):
        height = data["header"]["height"]
        existing = self.read_block_data(height)
        for existing_ver in existing:
            if self.same_block_data(data, existing_ver):
                return False
        # This version does not exist yet - write it out
        dname = self.height_to_dirname(height)
        if not os.path.exists(dname):
            print("Create new subdir: {}".format(dname))
            os.makedirs(dname)
        fname = "{}/{}".format(dname, height)
        fork_num = len(existing)
        if fork_num > 0:
            fname = fname + "_fork{}".format(fork_num)
        theFile = open(fname, "w")
        theFile.write(json.dumps(data))
        theFile.close()
        print("Wrote block data: {}".format(fname))
        return True
    
    # Read block data (all fork versions)
    def read_block_data(self, height):
        dname = self.height_to_dirname(height)
        block_data = []
        for fork in range(420):
            fname = "{}/{}".format(dname, height)
            if fork != 0:
                fname = fname + "_fork{}".format(fork)
            if not os.path.exists(fname):
                break
            theFile = open(fname, "r")
            data = theFile.readline()
            theFile.close()
            block_data = block_data + [json.loads(data)]
        return block_data

    # Compare if 2 blocks are the same
    def same_block_data(self, a, b):
        if a is None or b is None:
            return False
        return a["header"]["hash"] == b["header"]["hash"]

    # Get a block from the grin node and write it to a file
    def get_and_process_block(self, height):
        payload = {"jsonrpc": "2.0", "method": "get_block", "params": [height, None, None], "id": 1}
        try:
            req = requests.post(
                    self.grin_url,
                    auth=self.node_auth,
                    json=payload,
                    timeout=20)
            block = req.json()["result"]["Ok"]
            if int(block["header"]["height"]) != height:
                raise Exception("Failed to get block data from node")
        except Exception as e:
            print("Error: {}".format(e))
            raise
        return self.write_block_data(block)

    # Track the blockchain height
    def update_height(self, height):
        # Skip this if we are in the middle of an ipfs add
        if self.running_ipfs_add:
            return False
        h_file = self.block_data_dir + "/height"
        height = int(height)
        try:
            theFile = open(h_file, "r")
            jsn = theFile.read()
            theFile.close()
            existing_height = int(json.loads(jsn)["height"])
            if height <= existing_height:
                return
        except Exception as e:
            print("Failed to grok existing height file: {}".format(e))
        jsn = { "height": height }
        theFile = open(h_file, "w")
        theFile.write(json.dumps(jsn))
        theFile.close()

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
            print("added hash = {}".format(m[0]))
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

    # Is any scheduled scan running?
    def any_running(self):
        return self.full_scan_st["is_running"] or self.week_scan_st["is_running"] or self.day_scan_st["is_running"] or self.hour_scan_st["is_running"] or self.min_scan_st["is_running"]

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
        while chain_height is None:
            print("Failed to connect to grin node foreign api.  Will retry....")
            chain_height = self.get_chain_height()
            time.sleep(self.loop_delay)

        # Initialize scheduled scan status structures
        never = datetime(1970, 1, 1)
        self.full_scan_st = {"height":0, "date":never, "is_running": False}
        self.week_scan_st = {"height":chain_height-WEEK_BLOCKS, "date":never, "is_running": False}
        self.day_scan_st = {"height":chain_height-DAY_BLOCKS, "date":never, "is_running": False}
        self.hour_scan_st = {"height":chain_height-HOUR_BLOCKS, "date":never, "is_running": False}
        self.min_scan_st = {"height":chain_height-1, "date":never, "is_running": False}
        self.publish_st = {"dirty":False, "date":never}

        while True:
            # Init loop variables
            any_new = False      # Any new data written in this time around the loop?
        
            # Get current chain height
            chain_height = self.get_chain_height()
            if chain_height is None:
                print("Unable to contact node foreign api, will try again...")
                time.sleep(self.loop_delay)

            try:
                # Scan Full blockchain
                if datetime.now() - self.full_scan_st["date"] > timedelta(weeks=2):
                    self.full_scan_st["is_running"] = True
                    any_new = any_new or self.get_and_process_block(self.full_scan_st["height"])
                    if chain_height <= self.full_scan_st["height"] + 1:
                        self.full_scan_st["height"] = 0
                        self.full_scan_st["date"] = datetime.now()
                        self.full_scan_st["is_running"] = False
                        print("Completed Full Scan")
                    else:
                        self.full_scan_st["height"] = self.full_scan_st["height"] + 1

                # Scan blocks from the past Week
                if datetime.now() - self.week_scan_st["date"] > timedelta(days=3):
                    self.week_scan_st["is_running"] = True
                    any_new = any_new or self.get_and_process_block(self.week_scan_st["height"])
                    if chain_height <= self.week_scan_st["height"] + 1:
                        self.week_scan_st["height"] = chain_height - WEEK_BLOCKS
                        self.week_scan_st["date"] = datetime.now()
                        self.week_scan_st["is_running"] = False
                        print("Completed Week Scan")
                    else:
                        self.week_scan_st["height"] = self.week_scan_st["height"] + 1

                # Scan blocks from the past Day 
                if datetime.now() - self.day_scan_st["date"] > timedelta(hours=8):
                    self.day_scan_st["is_running"] = True
                    any_new = any_new or self.get_and_process_block(self.day_scan_st["height"])
                    if chain_height <= self.day_scan_st["height"] + 1:
                        self.day_scan_st["height"] = chain_height - DAY_BLOCKS
                        self.day_scan_st["date"] = datetime.now()
                        self.day_scan_st["is_running"] = False
                        print("Completed Day Scan")
                    else:
                        self.day_scan_st["height"] = self.day_scan_st["height"] + 1

                # Scan blocks from the last hour
                if datetime.now() - self.hour_scan_st["date"] > timedelta(hours=2):
                    self.hour_scan_st["is_running"] = True
                    any_new = any_new or self.get_and_process_block(self.hour_scan_st["height"])
                    if chain_height <= self.hour_scan_st["height"] + 1:
                        self.hour_scan_st["height"] = chain_height - HOUR_BLOCKS
                        self.hour_scan_st["date"] = datetime.now()
                        self.hour_scan_st["is_running"] = False
                        print("Completed Hour Scan")
                    else:
                        self.hour_scan_st["height"] = self.hour_scan_st["height"] + 1

                # Scan for new blocks continuously
                if datetime.now() - self.min_scan_st["date"] > timedelta(seconds=30):
                    if self.min_scan_st["height"] <= chain_height:
                        self.min_scan_st["is_running"] = True
                        any_new = any_new or self.get_and_process_block(self.min_scan_st["height"])
                        self.min_scan_st["date"] = datetime.now()
                        self.min_scan_st["height"] = self.min_scan_st["height"] + 1
                    else:
                        self.min_scan_st["is_running"] = False
        
                # Accounting
                if any_new:
                    self.update_height(self.min_scan_st["height"]-1)
                    self.publish_st["dirty"] = True
        
                # Launch a thread to Add new block data to IPFS and publish to IPNS
                if not self.any_running():
                    if self.publish_st["dirty"] and datetime.now() - self.publish_st["date"] > timedelta(hours=6):
                        print("add and publish to ipfs")
                        ipfs_add_thread = Thread(target = self.ipfs_add_and_publish)
                        ipfs_add_thread.daemon = True
                        ipfs_add_thread.start()
                        self.publish_st["dirty"] = False
                        self.publish_st["date"] = datetime.now()
        
                # Throttle polling for new blocks if no scan is actively running
                if not self.any_running():
                    time.sleep(self.loop_delay)
            except Exception as e:
                print("Error: {} - {}".format(e, traceback.format_exc()))


if __name__ == "__main__":
    explorer = IpfsExplorer()
    explorer.run()
