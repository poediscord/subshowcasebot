import json
import logging
import time
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
import praw
from prawcore.exceptions import InsufficientScope
from requests.exceptions import HTTPError


REQUIRED_SCOPES = ("edit", "flair", "identity", "modflair", "modlog", "modmail", "modposts", "privatemessages", "read", "submit")

class Config:
    def __init__(self, config):
        self.client_id = config["client_id"]
        self.client_secret = config["client_secret"]
        self.refresh_token = config["refresh_token"]
        self.user_agent = config["user_agent"]

        self.loglevel = config.get("loglevel", "INFO")

        self.sub_name = config["sub_name"]
        self.flair = config["flair"]

        self.max_delay = config.get("max_delay", 5) * 60 # in minutes
        self.pull_limit = config.get("pull_limit", 25)
        self.ignore_older = config.get("ignore_older", 2) # hours

        self.check_slow_delay = timedelta(minutes=config.get("check_slow_delay", 10))

        self.warn_delay = timedelta(minutes=config["warn"]["delay"]) # in minutes
        self.warn_message = config["warn"]["message"]

        self.remove_delay = timedelta(minutes=config["remove"]["delay"]) # in minutes
        self.remove_message = config["remove"]["message"]

        self.top_level_only_message = config["top_level_only"]["message"]


class State(Enum):
    CHECK = 0
    CHECK_SLOW = 1
    IGNORE = 2

class StateData:
    def __init__(self, state, noticed_at, last_check=None):
        self.state = state
        self.noticed_at = noticed_at
        self.last_check = last_check or noticed_at
        
states = {}
my_name = None

def load_config():
    config_location = os.environ.get("CONFIG_FILE", "instance/config.json")

    with open(config_location) as f:
        config = json.load(f)
    
    return Config(config)

def connect(config):
    global my_name
    
    log.info("Connecting to reddit")

    reddit = praw.Reddit(
        client_id=config.client_id,
        client_secret=config.client_secret,
        refresh_token=config.refresh_token,
        user_agent=config.user_agent
        )
    my_name = reddit.user.me(use_cache=True).name
    log.info(f"Connected as: {my_name}")
    return reddit


def monitor(reddit, subreddit, config):
    # we adjust the delay based on how often people are posting
    delay = config.max_delay

    start = datetime.now()
    ignore_before =  (start - timedelta(hours=config.ignore_older))
    log.debug(f"Ignoring posts older than: {ignore_before}")
    
    submitted_dates = []

    flair_delay = config.warn_delay + config.remove_delay

    log.debug(f"Check for flair changes often every {flair_delay}")

    log.debug("Scanning /new")
    # add new submissions we havnt seen to our check list
    for submission in subreddit.new(limit=config.pull_limit):
        sub_created = datetime.fromtimestamp(submission.created_utc)
        if sub_created >= ignore_before:
            found_submission(submission, start)

        submitted_dates.append(sub_created)

    # scans for posts where the mod changed the flair
    scan_mod_log(config, reddit, subreddit, action="editflair", details="flair_edit")

    log.debug("Checking submissions")
    # check our submissions
    for sub_id, sub_data in states.items():
        # possibly skip checking this submission for a bit
        if sub_data.state == State.CHECK or \
                (sub_data.state == State.CHECK_SLOW and \
                (sub_data.last_check + config.check_slow_delay) <= start):

            submission = reddit.submission(id=sub_id)

            sub_age = start - datetime.fromtimestamp(submission.created_utc)
            log.debug(f"Precheck submission {submission.id} age: {sub_age}")

            correct_flair = check_sub_flair(submission)
            meta_good = check_sub_meta(submission)
            
            if meta_good and correct_flair:
                sub_data.state = check_submission(config, submission, sub_age)
            elif meta_good and not correct_flair:
                # could in theory eventually have the correct flair, check slow if its an older post
                if sub_age > flair_delay:
                    log.debug(f"Submission {sub_id} could in theory eventually have the correct flair, check slow")
                    sub_data.state = State.CHECK_SLOW
                else:
                    log.debug(f"Submission {sub_id} has the wrong flair but is new, check often for flair updates")
                    sub_data.state = State.CHECK
            else:
                log.debug(f"Submission {sub_id} will never be relevent, ignore")
                sub_data.state = State.IGNORE

            sub_data.last_check = start
        else:
            if sub_data.state == State.CHECK_SLOW:
                log.debug(f"Submission {sub_id} skipped, check after {sub_data.last_check + config.check_slow_delay}")

    log.debug("Forgetting old submissions")
    # forget about submissions that are too old and that are not being checked normally
    to_remove = set()
    for sub_id, sub_data in states.items():
        if sub_data.state != State.CHECK and sub_data.noticed_at <= ignore_before:
            log.debug(f"Forgetting {sub_id}")
            to_remove.add(sub_id)

    log.debug(f"Forgetting {len(to_remove)} posts.")
    for sub_id in to_remove:
        del states[sub_id]

    log.debug(f"Posts remaining in cache: {len(states)}.")

    end = datetime.now()

    duration = end-start

    log.debug(f"Completed run in {duration}")
    
    # calculate how often to check,
    # we use the a quarter of the average amount of time it
    # takes the sub to make pull_limit threads minus how long we took

    if submitted_dates:
        tot_delta = sum(date.timestamp() for date in submitted_dates)
        avg_delta = tot_delta/len(submitted_dates)
        delay = min(config.max_delay, avg_delta*config.pull_limit/4 - duration.seconds)
    else:
        delay = config.max_delay
    
    # this in theory could be a delay of 0 but thats fine because
    # the library correctly rate limits anyway
    # with a limit of 25 and max delay of 5, it would take an average
    # delta of 0.8 minutes (75 threads per hour) to go under 5 minutes.
    log.debug(f"Sleeping for {delay} seconds")
    time.sleep( delay )
    
def check_sub_flair(submission):
    if hasattr(submission, "link_flair_template_id") and \
        submission.link_flair_template_id == config.flair:
        log.debug(f"Submission {submission.id} has correct flair ")
        # if its flaird right then we just want to know if its relevent
        return True
    else:
        log.debug(f"Submission {submission.id} has incorrect flair ")
        return False

def check_sub_meta(submission):
    if submission.is_self:
        log.debug(f"Submission {submission.id} is a self post, ignoring")
        return False

    if submission.approved:
        log.debug(f"Submission {submission.id} already approved, ignoring")
        return False

    if submission.author is None:
        log.debug(f"Submission {submission.id} deleted, ignoring")
        return False

    log.debug(f"Submission {submission.id}'s meta seems relevent")
    return True

def found_submission(submission, noticed_at):
    if submission.id not in states:
        log.debug(f"Found {submission.id}")
        states[submission.id] = StateData(State.CHECK, noticed_at)

def scan_mod_log(config, reddit, subreddit, action, details=None, mod=None):
    if not details:
        details = action

    now = datetime.now()

    ignore_before = (now - timedelta(hours=config.ignore_older)).timestamp()
    
    log.debug("Scanning mod log")
    # add reflaired/removed submissions that we havnt seen or that we saw but ignored
    for mod_action in subreddit.mod.log(action=action, mod=mod, limit=config.pull_limit):
        if mod_action.created_utc > ignore_before:
            # we use the date the mod action was created, this allows mods to put a post back in the system
            if mod_action.details == details:
                # check for both flair edits AND 
                # try to put our removed threads back in our cache after a restart
                # the mod action just has a link to the actual target submission :U
                submission = reddit.submission(url=f"http://www.reddit.com{mod_action.target_permalink}")
                found_submission(submission, now)

def check_submission(config, submission, age):

    log.debug(f"Checking {submission.id}")

    removed = False
    if hasattr(submission, "banned_by") and submission.banned_by is not None:
        removed = True
        if submission.banned_by != my_name:
            log.debug(f"Submission {submission.id} was removed by someone else, ignoring")
            return State.IGNORE
        else:
            log.debug(f"Submission {submission.id} was removed by us")

    sub_comment, warning_comment, warn_age = get_comments(submission)

    if not sub_comment:
        log.debug(f"Submission {submission.id} hasnt commented yet")

        if age >= config.warn_delay:
            log.debug(f"Submission {submission.id} old enough to be warned")

            log.debug(f"Submission {submission.id} already warned: {warning_comment!=None} when: {warn_age} ago")

            if warning_comment:
                check_tell_user_top_level_only(submission, warning_comment)

            if removed and warn_age >= config.remove_delay:
                # post removed [by us] and user still hasnt commented, keep it removed (until they comment)
                # we will continue to check often for a little bit of time after their post was removed.
                log.debug(f"Submission {submission.id} already removed")
                return State.CHECK_SLOW
            elif not warning_comment:
                # has not been warned yet
                log.debug(f"Submission {submission.id} sending warning")
                warn_submission(submission, config.warn_message)
                # recheck often for their comment
                return State.CHECK
            elif warn_age and warn_age >= config.remove_delay:
                # submitter was warned long enough ago and didnt do anything
                log.debug(f"Submission {submission.id} removing")
                remove_submission(submission, warning_comment, config.remove_message)
                # check quick for now, after the grace period will will check slow
                return State.CHECK
            else:
                # post warned but not removed yet, they might comment soon
                if removed:
                    log.debug(f"Submission {submission.id} already removed but we are still in the quick check grace period")
                return State.CHECK
        else:
            # thread too young, check often for changes
            log.debug(f"Submission {submission.id} is too new to do anything about.")
            return State.CHECK
    else:
        # user has commented undo all our work
        log.debug(f"Submission {submission.id} comment posted, approve and ignore.")
        approve_submission(submission, warning_comment, removed)
        # never check again
        return State.IGNORE
        
def get_comments(submission):
    submitters_comment = None
    my_comment = None
    my_comment_age = None

    submitter_name = submission.author.name
    for comment in submission.comments:
        # deleted comments have no author
        if not comment.author:
            continue

        if comment.author.name == submitter_name:
            submitters_comment = comment
        elif comment.author.name == my_name:
            my_comment = comment
            my_comment_age = datetime.now() - datetime.fromtimestamp(comment.created_utc)
        if my_comment and submitters_comment:
            break

    return submitters_comment, my_comment, my_comment_age

def check_tell_user_top_level_only(submission, my_comment):
    user_reply = check_replied_to_comment(my_comment, submission.author.name)
    if user_reply:
        log.debug(f"Submitter of {submission.id} replied to the bot")
        my_reply = check_replied_to_comment(my_comment, my_name)
        if not my_reply:
            tell_user_top_level_only(submission, user_reply)
        else:
            log.debug(f"Submitter of {submission.id} already told to make a top level comment.")


def check_replied_to_comment(parent_comment, name):
    parent_comment.replies.replace_more(limit=2)
    # 'MoreComments' objects need to be dealt with/loaded sometimes
    for comment in parent_comment.replies:
        if isinstance(comment, praw.models.MoreComments):
            # for some reason a ton of replies were made and we didnt load them all
            log.warning(f"Comment {parent_comment.id} had extra many MoreComment links")
            continue

        if comment.author.name == name:
            return comment

    return None

def tell_user_top_level_only(submission, user_reply):
    log.debug(f"Tell Submitter of {submission.id} to make a top level comment instead.")
    my_reply = user_reply.reply(config.top_level_only_message)
    my_reply.mod.distinguish()
    return my_reply

def remove_submission(submission, warning_comment, message):
    log.info(f"Removing {submission.id}")
    warning_comment.delete()
    submission.mod.remove()
    submission.mod.send_removal_message(message)

def warn_submission(submission, message):
    log.debug(f"Posting reply to {submission.id}")
    comment = submission.reply(message)
    comment.mod.distinguish(sticky=True)

def approve_submission(submission, warning_comment, removed):
    if removed:
        log.info(f"Approving {submission.id}")
        submission.mod.approve()
    if warning_comment:
        log.debug("Removing our comment.")
        warning_comment.delete()

def utc_to_local(utc_dt):
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(tz=None)

if __name__ == "__main__":
    config = load_config()

    logging.basicConfig(format='%(asctime)s %(name)s:%(levelname)s:%(message)s',
                        datefmt='%y-%m-%d %H:%M:%S')
    log = logging.getLogger("subshowcasebot")
    log.setLevel(config.loglevel)
    log.info(f"Starting showcase bot with log level: {log.level}")

    try:
        reddit = connect(config)
        error_count = 0

        log.info(f"Getting subreddit {config.sub_name}")
        subreddit = reddit.subreddit(config.sub_name)

        # reload links we removed
        log.debug("Reloading some cache")
        scan_mod_log(config, reddit, subreddit, action="remove", mod=reddit.user.me())

        while True:
            try:
                monitor(reddit, subreddit, config)

                error_count = 0
            except praw.exceptions.APIException:
                log.error(f"PRAW raised an API exception! Logging but ignoring.", exc_info=True)
                error_count = min(10, error_count)
            except HTTPError:
                log.error(f"requests raised an exception! Logging but ignoring.", exc_info=True)
                error_count = min(10, error_count)

            # in the case of an error, sleep longer and longer
            # one error, retry right away
            # more than one, delay a minute per consecutive error.
            # when reddit is down, this value will go up
            # when its just something like we cant reply to this deleted comment, try again right away
            time.sleep(max(0,error_count)*60)
    except InsufficientScope as e:
        log.error(f"PRAW raised InsufficientScope! Make sure you have the following scopes: {','.join(REQUIRED_SCOPES)}")
        raise e