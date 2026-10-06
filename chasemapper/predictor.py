#!/usr/bin/env python
#
#   Project Horus - Browser-Based Chase Mapper - Predictor
#
#   Copyright (C) 2018  Mark Jessop <vk5qi@rfhead.net>
#   Released under GNU GPL v3 or later
#
import logging
import shlex
import subprocess
from threading import Thread

model_download_running = False


def predictor_download_model(command, callback):
    """ Run the supplied command, which should download a GFS model and place it into the GFS directory 

    When the downloader completes, or if an error is thrown, the status is passed to a callback function.
    """
    global model_download_running

    if model_download_running:
        return

    model_download_running = True

    try:
        ret_code = subprocess.call(shlex.split(command))
    except Exception as e:
        # Something broke when running the detection function.
        logging.error("Error when attempting to download model - %s" % (str(e)))
        model_download_running = False
        callback("Error - See log.")
        return

    model_download_running = False

    if ret_code == 0:
        logging.info("Model Download Completed.")
        callback("OK")
        return
    else:
        logging.error("Model Downloader returned code %d" % ret_code)
        callback("Error: Ret Code %d" % ret_code)
        return


def predictor_spawn_download(command, callback=None):
    """ Spawn a model downloader in a new thread """

    if model_download_running:
        return "Already Downloading."

    _download_thread = Thread(
        target=predictor_download_model,
        kwargs={"command": command, "callback": callback},
    )
    _download_thread.start()

    return "Started downloader."

