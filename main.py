# coding: utf-8
# Copyright © 2014-2019 VMware, Inc. All Rights Reserved.

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import List, Optional

import humanfriendly
import psycopg2
# noinspection PyPackageRequirements
import yara
from celery import group
from peewee import SqliteDatabase

import globals
import singleton
from analysis_result import AnalysisResult
from binary_database import BinaryDetonationResult, db
from config_handling import ConfigurationInit
from exceptions import SingleInstanceException
from feed import CbFeed, CbFeedInfo, CbReport
from tasks import analyze_binary, generate_rule_map, update_yara_rules_remote
from utilities import placehold

logging_format = "%(asctime)s-%(name)s-%(lineno)d-%(levelname)s-%(message)s"
logging.basicConfig(format=logging_format)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

celery_logger = logging.getLogger("celery.app.trace")
celery_logger.setLevel(logging.ERROR)


def generate_feed_from_db() -> None:
    """
    Creates a feed based on specific database information.
    """
    query = BinaryDetonationResult.select().where(BinaryDetonationResult.score > 0)

    reports = []
    for binary in query:
        fields = {
            "iocs": {"md5": [binary.md5]},
            "score": binary.score,
            "timestamp": int(time.mktime(time.gmtime())),
            "link": "",
            "id": "binary_{0}".format(binary.md5),
            "title": binary.last_success_msg,
            "description": binary.last_success_msg,
        }
        reports.append(CbReport(**fields))

    feedinfo = {
        "name": "yara",
        "display_name": "Yara",
        "provider_url": "http://plusvic.github.io/yara/",
        "summary": "Scan binaries collected by Carbon Black with Yara.",
        "tech_data": "There are no requirements to share any data with Carbon Black to use this feed.",
        "icon": "yara-logo.png",
        "category": "Connectors",
    }
    feedinfo = CbFeedInfo(**feedinfo)
    feed = CbFeed(feedinfo, reports)

    logger.debug("Writing out feed '{0}' to disk".format(feedinfo.data["name"]))
    with open(globals.g_output_file, "w") as fp:
        fp.write(feed.dump())


def generate_yara_rule_map_hash(yara_rule_path: str) -> None:
    """
    Create a list of hashes for each yara rule.

    :param yara_rule_path: the path to where the yara rules are stored.
    """
    temp_list = []
    for fn in os.listdir(yara_rule_path):
        if fn.lower().endswith(".yar") or fn.lower().endswith(".yara"):
            fullpath = os.path.join(yara_rule_path, fn)
            if not os.path.isfile(fullpath):
                continue
            with open(os.path.join(yara_rule_path, fn), "rb") as fp:
                data = fp.read()
                md5 = hashlib.md5()
                md5.update(data)
                temp_list.append(str(md5.hexdigest()))

    globals.g_yara_rule_map_hash_list = temp_list
    globals.g_yara_rule_map_hash_list.sort()


def generate_rule_map_remote(yara_rule_path: str) -> None:
    """
    Get remote rules and store into an internal map keyed by file name.

    :param yara_rule_path: path to where the rules are stored
    """
    ret_dict = {}
    for fn in os.listdir(yara_rule_path):
        if fn.lower().endswith(".yar") or fn.lower().endswith(".yara"):
            fullpath = os.path.join(yara_rule_path, fn)
            if not os.path.isfile(fullpath):
                continue
            with open(os.path.join(yara_rule_path, fn), "rb") as fp:
                ret_dict[fn] = fp.read()

    result = update_yara_rules_remote.delay(ret_dict)
    globals.g_yara_rule_map = ret_dict
    while not result.ready():
        time.sleep(0.1)


def analyze_binaries(md5_hashes: List[str], local: bool) -> Optional:
    """
    Analyze binaries.

    TODO: determine return typing!

    :param md5_hashes: list of  hashes to check.
    :param local: True if local
    :return: None if there is a problem; results otherwise
    """
    if local:
        try:
            results = []
            for md5_hash in md5_hashes:
                results.append(analyze_binary(md5_hash))
        except Exception as err:
            logger.error("{0}".format(err))
            time.sleep(5)
            return None
        else:
            return results
    else:
        try:
            scan_group = []
            for md5_hash in md5_hashes:
                scan_group.append(analyze_binary.s(md5_hash))
            job = group(scan_group)

            result = job.apply_async()

            start = time.time()
            while not result.ready():
                if time.time() - start >= 120:  # 2 minute timeout
                    break
                else:
                    time.sleep(0.1)
        except Exception as err:
            logger.error("Error when analyzing: {0}".format(err))
            logger.error(traceback.format_exc())
            time.sleep(5)
            return None
        else:
            if result.successful():
                return result.get(timeout=30)
            else:
                return None


def save_results(analysis_results: List[AnalysisResult]) -> None:
    """
    Save the current analysis results.

    :param analysis_results: list of AnalysisResult objects
    """
    for analysis_result in analysis_results:
        if analysis_result.binary_not_available:
            globals.g_num_binaries_not_available += 1
            continue

        bdr, created = BinaryDetonationResult.get_or_create(md5=analysis_result.md5)

        try:
            bdr.md5 = analysis_result.md5
            bdr.last_scan_date = datetime.now()
            bdr.score = analysis_result.score
            bdr.last_error_msg = analysis_result.last_error_msg
            bdr.last_success_msg = analysis_result.short_result
            bdr.misc = json.dumps(globals.g_yara_rule_map_hash_list)
            bdr.save()
            globals.g_num_binaries_analyzed += 1
        except Exception as err:
            logger.error("Error saving to database: {0}".format(err))
            logger.error(traceback.format_exc())
        else:
            if analysis_result.score > 0:
                generate_feed_from_db()


def get_database_conn():
    """
    Generate a database connection based on our postgres settings.

    :return: database connection
    """
    logger.info("Connecting to Postgres database...")
    conn = psycopg2.connect(
        host=globals.g_postgres_host,
        database=globals.g_postgres_db,
        user=globals.g_postgres_username,
        password=globals.g_postgres_password,
        port=globals.g_postgres_port,
    )

    return conn


def get_cursor(conn, start_date_binaries: datetime):
    """
    Get a query qursor into the database.

    :param conn: database connection
    :param start_date_binaries: Timestamp representing the earliest time to check for binaries
    :return: cursor pointing to the query results
    """
    cur = conn.cursor(name="yara_agent")

    # noinspection SqlDialectInspection,SqlNoDataSourceInspection
    cur.execute(
        "SELECT md5hash FROM storefiles WHERE present_locally = TRUE AND timestamp >= '{0}' "
        "ORDER BY timestamp DESC".format(start_date_binaries)
    )

    return cur


def execute_script() -> None:
    """
    Execute a external maintenence script (vacuum script).
    """
    logger.info("!!!Executing vacuum script!!!")

    prog = subprocess.Popen(globals.g_vacuum_script, shell=True, universal_newlines=True)
    stdout, stderr = prog.communicate()
    if stdout is not None and len(stdout.strip()) > 0:
        logger.info(stdout)
    if stderr is not None and len(stderr.strip()) > 0:
        logger.error(stderr)
    if prog.returncode:
        logger.warning(f"program returned error code {prog.returncode}")
    logger.info("!!!Done Executing vacuum script!!!")


def perform(yara_rule_dir: str) -> None:
    """
    Perform a yara search.

    :param yara_rule_dir: location of the rules directory
    """
    if globals.g_remote:
        logger.info("Uploading yara rules to workers...")
        generate_rule_map_remote(yara_rule_dir)

    num_total_binaries = 0
    num_binaries_skipped = 0
    num_binaries_queued = 0
    md5_hashes = []

    start_time = time.time()

    # Determine our binaries window (date forward)
    start_datetime = datetime.now()
    start_date_binaries = start_datetime - timedelta(days=globals.g_num_days_binaries)

    # make the connection, get the info, get out
    conn = get_database_conn()
    cur = get_cursor(conn, start_date_binaries)
    rows = cur.fetchall()
    conn.commit()
    conn.close()

    logger.info(f"Enumerating modulestore...found {len(rows)} resident binaries")
    for row in rows:
        seconds_since_start = (datetime.now() - start_datetime).seconds
        if seconds_since_start >= globals.g_vacuum_seconds > 0:
            execute_script()
            start_datetime = datetime.now()

        num_total_binaries += 1
        md5_hash = row[0].hex()

        num_binaries_queued += 1

        if _check_hash_against_feed(md5_hash):
            md5_hashes.append(md5_hash)
        else:
            num_binaries_skipped += 1

        # if we hit our hash chunking limit, save and reset
        if len(md5_hashes) >= globals.g_max_hashes:
            _analyze_save_and_log(md5_hashes, start_time, num_binaries_skipped, num_total_binaries)
            md5_hashes = []

    # any finishup work
    if len(md5_hashes) > 0:
        _analyze_save_and_log(md5_hashes, start_time, num_binaries_skipped, num_total_binaries)

    generate_feed_from_db()


def _check_hash_against_feed(md5_hash: str) -> bool:
    """
    Check if the found hash matches our feed criteria.

    :param md5_hash: hash to be checked
    :return: True if the binary does not exist
    """
    query = BinaryDetonationResult.select().where(BinaryDetonationResult.md5 == md5_hash)

    if query.exists():
        try:
            bdr = BinaryDetonationResult.get(BinaryDetonationResult.md5 == md5_hash)
            scanned_hash_list = json.loads(bdr.misc)
            if globals.g_disable_rescan and bdr.misc:
                return False

            # If it is the same then we don't need to scan again
            if scanned_hash_list == globals.g_yara_rule_map_hash_list:
                return False
        except Exception as err:
            logger.error(f"Unable to decode yara rule map hash from database: {err}")
            return False
    return True


def _analyze_save_and_log(hashes: List[str], start_time: float, num_binaries_skipped: int,
                          num_total_binaries: int) -> None:
    """
    Analyise and save any found binaries.

    :param hashes: List of hashes
    :param start_time: start time of the operation (python time)
    :param num_binaries_skipped: numb er of binaries skipped for any reason
    :param num_total_binaries: number of binaries seen
    """
    analysis_results = analyze_binaries(hashes, local=(not globals.g_remote))
    if analysis_results:
        for analysis_result in analysis_results:
            logger.debug((f"Analysis result is {analysis_result.md5} {analysis_result.binary_not_available}"
                          f" {analysis_result.long_result} {analysis_result.last_error_msg}"))
            if analysis_result.last_error_msg:
                logger.error(analysis_result.last_error_msg)
        save_results(analysis_results)

    _rule_logging(start_time, num_binaries_skipped, num_total_binaries)


def _rule_logging(start_time: float, num_binaries_skipped: int, num_total_binaries: int) -> None:
    """
    Simple method to log yara work.

    :param start_time: start time for the work
    :param num_binaries_skipped: numb er of binaries skipped for any reason
    :param num_total_binaries: number of binaries seen
    """
    elapsed_time = time.time() - start_time
    logger.info("elapsed time: {0}".format(humanfriendly.format_timespan(elapsed_time)))
    logger.debug(f"   number binaries scanned: {globals.g_num_binaries_analyzed}")
    logger.debug(f"   number binaries already scanned: {num_binaries_skipped}")
    logger.debug(f"   number binaries unavailable: {globals.g_num_binaries_not_available}")
    logger.info(f"total binaries from db: {num_total_binaries}")
    logger.debug("   binaries per second: {0}:".format(round(num_total_binaries / elapsed_time, 2)))
    overzero = len(BinaryDetonationResult.select().where(BinaryDetonationResult.score > 0))
    logger.info(f"num binaries score greater than zero: {overzero}\n")


################################################################################
# Main entrypoint
################################################################################

def handle_arguments():
    """
    Setup the main program options.

    :return: parsed arguments
    """
    parser = argparse.ArgumentParser(description="Yara Agent for Yara Connector")
    parser.add_argument(
        "--config-file",
        required=True,
        default="yara_agent.conf",
        help="Location of the config file",
    )
    parser.add_argument(
        "--log-file",
        default="{YARA}/local/yara_agent.log",
        help="Log file output (defaults to `local` folder)"
    )
    parser.add_argument(
        "--output-file",
        default="{YARA}/local/yara_feed.json",
        help="output feed file (defaults to `local` folder)"
    )
    parser.add_argument(
        "--validate-yara-rules",
        action="store_true",
        help="ONLY validate yara rules in a specified directory",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Provide additional logging"
    )

    return parser.parse_args()


def main():
    """
    Main execution function.  Script will exit with a non-zero value based on the following:
        1: Not the only instance running
        2: Configuration problem
        3: User interrupt
        4: Unexpected Yara scan exception
        5: Yara rule validation problem
    """
    try:
        singleton.SingleInstance()
    except SingleInstanceException as err:
        logger.error(f"Only one instance of this script is allowed to run at a time: {err}")
        sys.exit(1)

    args = handle_arguments()
    if args.debug:
        logger.setLevel(logging.DEBUG)

    if args.log_file:
        use_log_file = os.path.abspath(os.path.expanduser(placehold(args.log_file)))
        formatter = logging.Formatter(logging_format)
        handler = logging.handlers.RotatingFileHandler(use_log_file, maxBytes=10 * 1000000, backupCount=10)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    else:
        use_log_file = None

    # Verify the configuration file and load up important global variables
    try:
        ConfigurationInit(args.config_file, use_log_file)
    except Exception as err:
        logger.error(f"Unable to continue due to a configuration problem: {err}")
        sys.exit(2)

    if args.validate_yara_rules:
        logger.info(f"Validating yara rules in directory: {globals.g_yara_rules_dir}")
        yara_rule_map = generate_rule_map(globals.g_yara_rules_dir)
        try:
            yara.compile(filepaths=yara_rule_map)
            logger.info("All yara rules compiled successfully")
        except Exception as err:
            logger.error(f"There were errors compiling yara rules: {err}\n{traceback.format_exc()}")
            sys.exit(5)
    else:
        try:
            globals.g_yara_rule_map = generate_rule_map(globals.g_yara_rules_dir)
            generate_yara_rule_map_hash(globals.g_yara_rules_dir)
            database = SqliteDatabase(os.path.join(globals.g_feed_database_dir, "binary.db"))
            db.initialize(database)
            db.connect()
            db.create_tables([BinaryDetonationResult])
            generate_feed_from_db()
            perform(globals.g_yara_rules_dir)
        except KeyboardInterrupt:
            logger.info("\n\n##### Interupted by User!\n")
            sys.exit(3)
        except Exception as err:
            logger.error(f"There were errors executing yara rules: {err}\n{traceback.format_exc()}")
            sys.exit(4)


if __name__ == "__main__":
    main()
