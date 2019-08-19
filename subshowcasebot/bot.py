import json
import logging
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
import praw
from prawcore.exceptions import InsufficientScope


REQUIRED_SCOPES = ("edit", "flair", "identity", "modflair", "modlog", "modmail", "modposts", "privatemessages", "read", "submit")

class State(Enum):
    CHECK = 0
    SHOWCASE = 1
    WARNED = 2
    REMOVED = 3
    COMPLETE = 4
    IGNORE = 5

class StateData:
    def __init__(self, state, created):
        self.state = state
        self.created = created

def connect(config):
    reddit = praw.Reddit(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        refresh_token=config["refresh_token"],
        user_agent=config["user_agent"]
        )
    return reddit


def monitor(config):
    log.info("Connecting to reddit")
    reddit = connect(config)
    my_name = reddit.user.me(use_cache=True).name
    log.info(f"Connected as: {my_name}")
    
    sub_name = config["sub_name"]
    
    max_delay = config.get("max_delay", 5) * 60 # in minutes
    pull_limit = config.get("pull_limit", 25)
    ignore_older = config.get("ignore_older", 2) # hours

    states = {}

    log.info(f"Getting subreddit {sub_name}")
    subreddit = reddit.subreddit(sub_name)

    # we adjust the delay based on how often people are posting
    delay = max_delay

    # reload links we removed
    ignore_before = (datetime.now() - timedelta(hours=ignore_older)).timestamp()
    log.debug("Reloading some cache from removed links")
    for mod_action in subreddit.mod.log(action="remove", mod=reddit.user.me(), limit=pull_limit):
        if mod_action.created_utc > ignore_before:
            if mod_action.details == "remove":
                submission = reddit.submission(url=f"http://www.reddit.com{mod_action.target_permalink}")
                log.debug(f"Found {submission.id}")
                if (submission.id not in states) or (submission.id in states and states[submission.id] == State.IGNORE):
                    states[submission.id] = StateData(State.CHECK, submission.created_utc)

    while True:
        start = datetime.now()
        ignore_before =  (start - timedelta(hours=ignore_older))
        log.debug(f"Ignoring posts older than: {ignore_before}")
        ignore_before = ignore_before.timestamp()
        
        submitted_dates = []

        log.debug("Scanning /new")
        # add new submissions we havnt seen to our check list
        for submission in subreddit.new(limit=pull_limit):
            if submission.created_utc > ignore_before:
                if submission.id not in states:
                    states[submission.id] = StateData(State.CHECK, submission.created_utc)
                    log.debug(f"Found {submission.id}")

            submitted_dates.append(submission.created_utc)

        log.debug("Scanning mod log")
        # add reflaired submissions that we havnt seen or that we saw but ignored
        for mod_action in subreddit.mod.log(action="editflair", limit=pull_limit):
            if mod_action.created_utc > ignore_before:
                if mod_action.details == "flair_edit":
                    # check for both flair edits AND 
                    # try to put our removed threads back in our cache after a restart
                    # the mod action just has a link to the actual target submission :U
                    submission = reddit.submission(url=f"http://www.reddit.com{mod_action.target_permalink}")
                    if (submission.id not in states) or (submission.id in states and states[submission.id] == State.IGNORE):
                        log.debug(f"Found {submission.id}")
                        states[submission.id] = StateData(State.CHECK, submission.created_utc)

        log.debug("Checking submissions")
        # check our submissions
        for sub_id, sub_data in states.items():
            if sub_data.state not in (State.COMPLETE, State.IGNORE):
                submission = reddit.submission(id=sub_id)
                sub_data.state = check_submission(config, reddit, submission)

        log.debug("Forgetting old submissions")
        # forget about submissions that are too old
        to_remove = set()
        for sub_id, sub_data in states.items():
            if sub_data.created <= ignore_before:
                log.debug(f"Forgetting {sub_id}")
                to_remove.add(sub_id)

        for sub_id in to_remove:
            del states[sub_id]

        end = datetime.now()

        duration = end-start

        log.debug(f"Completed loop in {duration} seconds")
        
        # calculate how often to check,
        # we use the a quarter of the average amount of time it
        # takes the sub to make pull_limit threads minus how long we took

        if submitted_dates:
            tot_delta = sum(submitted_dates)
            avg_delta = tot_delta/len(submitted_dates)
            delay = min(max_delay, avg_delta*pull_limit/4 - duration.seconds)
        else:
            delay = max_delay
        
        # this in theory could be a delay of 0 but thats fine because
        # the library correctly rate limits anyway
        # with a limit of 25 and max delay of 5, it would take an average
        # delta of 0.8 minutes (75 threads per hour) to go under 5 minutes.
        log.debug(f"Sleeping for {delay} seconds")
        time.sleep( delay )

def check_submission(config, reddit, submission):
    flair = config["flair"]

    warn_delay = config["warn"]["delay"] * 60 # in minutes
    warn_message = config["warn"]["message"]

    remove_delay = config["remove"]["delay"] * 60 # in minutes
    remove_message  = config["remove"]["message"]

    log.debug(f"Checking {submission.id}")

    if hasattr(submission, "link_flair_template_id") and \
        submission.link_flair_template_id == flair and \
        not submission.is_self and \
        not submission.approved and \
        submission.author is not None:
        # only look at posts that are untouched, flaired and not self
        # AND that were not deleted by the author

        log.debug(f"Submission is showcase")
        age = datetime.now().timestamp() - submission.created_utc
        log.debug(f"Submission age: {age} seconds")

        removed = False
        if hasattr(submission, "banned_by") and submission.banned_by is not None:
            removed = True
            if submission.banned_by != reddit.user.me(use_cache=True).name:
                log.debug(f"Submission was removed by someone else ({submission.banned_by}), ignoring")
                return State.IGNORE

        if age >= warn_delay:
            log.debug("Submission old enough to be warned")

            warning_comment, warn_age = was_warned(reddit, submission)
            log.debug(f"Submission warned?: {warning_comment!=False}")

            if not has_comment(submission):
                log.debug("Submitter hasnt commented yet")
                # user hasnt commented yet

                if removed:
                    # post removed and user hasnt commented, keep it removed
                    log.debug("Submission already removed")
                    return State.REMOVED
                elif not warning_comment:
                    log.debug("Warning Submission")
                    # has not been warned yet
                    warn_submission(reddit, submission, warn_message)
                    return State.WARNED
                elif warn_age and \
                    warn_age >= remove_delay:
                    log.debug("Removing Submission")
                    # submitter was warned long enough ago and didnt do anything
                    remove_submission(reddit, submission, warning_comment, remove_message)
                    return State.REMOVED
            else:
                log.debug("Submitter commented")
                # user has commented undo all our work
                if warning_comment:
                    log.debug("Approving submission")
                    approve_warned_submission(reddit, submission, warning_comment, removed)

                return State.COMPLETE
        return State.CHECK
    else:
        log.debug("Submission irrelevent")
        return State.IGNORE

        
def has_comment(submission):
    submitter_name = submission.author.name
    for comment in submission.comments:
        if comment.author.name == submitter_name:
            return True
    return False

def was_warned(reddit, submission):
    my_name = reddit.user.me(use_cache=True).name
    for comment in submission.comments:
        if comment.author.name == my_name:
            created_date = comment.created_utc
            age = datetime.now().timestamp() - created_date
            return comment, age
    return False, False

def remove_submission(reddit, submission, warning_comment, message):
    log.info(f"Removing {submission.id}")
    warning_comment.delete()
    submission.mod.remove()
    submission.mod.send_removal_message(message)

def warn_submission(reddit, submission, message):
    log.debug(f"Posting reply to {submission.id}")
    comment = submission.reply(message)
    comment.mod.distinguish(sticky=True)

def approve_warned_submission(reddit, submission, warning_comment, removed):
    if removed:
        log.info(f"Approving {submission.id}")
        submission.mod.approve()
    if warning_comment:
        log.debug("Removing our comment.")
        warning_comment.delete()

def utc_to_local(utc_dt):
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(tz=None)

if __name__ == "__main__":
    with open("instance/config.json") as f:
        config = json.load(f)

    logging.basicConfig(format='%(asctime)s %(name)s:%(levelname)s:%(message)s',
                        datefmt='%y-%m-%d %H:%M:%S')
    log = logging.getLogger("subshowcasebot")
    log.setLevel(config.get("loglevel", "WARNING"))
    log.info(f"Starting showcase bot with log level: {log.level}")

    try:
        monitor(config)
    except InsufficientScope as e:
        log.error(f"PRAW raised InsufficientScope! Make sure you have the following scopes: {','.join(REQUIRED_SCOPES)}")