#!/bin/bash
journalctl -u knowledge-base --no-pager -n 50 | grep -iE "webhook|challenge|verification|POST"
