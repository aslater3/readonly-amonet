import datetime
import os

def log(s):
    line = "[{}] {}".format(datetime.datetime.now(), s)
    print(line)

    log_path = os.environ.get("AMONET_LOG_FILE", "amonet.log")
    with open(log_path, "a") as fout:
        fout.write(line + "\n")
