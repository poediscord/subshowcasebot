import json
import logging
import time
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
import praw
from prawcore.exceptions import InsufficientScope


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

        self.slow_check_delay = timedelta(minutes=config.get("slow_check_delay", 10))

        self.warn_delay = timedelta(minutes=config["warn"]["delay"]) # in minutes
        self.warn_message = config["warn"]["message"]

        self.remove_delay = timedelta(minutes=config["remove"]["delay"]) # in minutes
        self.remove_message = config["remove"]["message"]


class State(Enum):
    CHECK = 0
    WRONG_FLAIR = 1
    IGNORE = 2

class StateData:
    def __init__(self, state, created):
        self.state = state
        self.created = created
        self.check_after = None
        
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


def monitor(reddit, config):
    log.info(f"Getting subreddit {config.sub_name}")
    subreddit = reddit.subreddit(config.sub_name)

    # we adjust the delay based on how often people are posting
    delay = config.max_delay

    flair_buffer_delay = config.warn_delay + config.remove_delay

    # reload links we removed
    log.debug("Reloading some cache")
    scan_mod_log(config, reddit, subreddit, action="remove", mod=reddit.user.me())

    while True:
        start = datetime.now()
        ignore_before =  (start - timedelta(hours=config.ignore_older))
        log.debug(f"Ignoring posts older than: {ignore_before}")
        
        submitted_dates = []

        log.debug("Scanning /new")
        # add new submissions we havnt seen to our check list
        for submission in subreddit.new(limit=config.pull_limit):
            sub_created = datetime.fromtimestamp(submission.created_utc)
            if sub_created >= ignore_before:
                found_submission(submission)

            submitted_dates.append(sub_created)

        # scans for posts where the mod changed the flair
        scan_mod_log(config, reddit, subreddit, action="editflair", details="flair_edit")

        log.debug("Checking submissions")
        # check our submissions
        for sub_id, sub_data in states.items():
            # possibly skip checking this submission for a bit
            if not sub_data.check_after or sub_data.check_after <= start:    
                if sub_data.state != State.IGNORE:
                    submission = reddit.submission(id=sub_id)

                    # see if we care about this submission at all
                    sub_data.state = check_sub_relevent(submission)

                    if sub_data.state == State.CHECK or \
                        (sub_data.state == State.WRONG_FLAIR and (start-sub_data.created) < flair_buffer_delay):
                        # recheck if we are supposed to check or if the thread is slow_check_delay old
                        # that way people that change their flair quickly get picked up
                        sub_data.state, sub_delay_for = check_submission(config, submission)
                        if sub_delay_for:
                            sub_data.check_after = start + sub_delay_for
                            log.debug(f"Submission {submission.id} 's next check delayed until after {sub_data.check_after}")
                        else:
                            sub_data.check_after = None
                    elif sub_data.state == State.WRONG_FLAIR:
                        sub_data.check_after = start + config.slow_check_delay
                    # otherwise the submission became irrelevent
            else:
                log.debug(f"Submission {submission.id} skipped, check after {sub_data.check_after}")

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

        log.debug(f"Completed loop in {duration}")
        
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
    
def check_sub_relevent(submission):
    if hasattr(submission, "link_flair_template_id") and \
        submission.link_flair_template_id == config.flair:
        log.debug(f"Submission {submission.id} has correct flair ")
        # if its flaird right then we just want to know if its relevent
        return check_sub_meta(submission)
    else:
        log.debug(f"Submission {submission.id} has incorrect flair ")
        if check_sub_meta(submission) == State.CHECK:
            # thread might become relevent if they change the flair later
            return State.WRONG_FLAIR
        else:
            log.debug(f"Submission {submission.id} wont ever be relevent")
            # thread wont ever be relevent again
            return State.IGNORE

def check_sub_meta(submission):
    if submission.is_self:
        log.debug(f"Submission {submission.id} is a self post, ignoring")
        return State.IGNORE

    if submission.approved:
        log.debug(f"Submission {submission.id} already approved, ignoring")
        return State.IGNORE

    if submission.author is None:
        log.debug(f"Submission {submission.id} deleted, ignoring")
        return State.IGNORE

    return State.CHECK

def found_submission(submission):
    if submission.id not in states:
        log.debug(f"Found {submission.id}")
        states[submission.id] = StateData(State.CHECK, datetime.fromtimestamp(submission.created_utc))

def scan_mod_log(config, reddit, subreddit, action, details=None, mod=None):
    if not details:
        details = action

    ignore_before = (datetime.now() - timedelta(hours=config.ignore_older)).timestamp()
    
    log.debug("Scanning mod log")
    # add reflaired/removed submissions that we havnt seen or that we saw but ignored
    for mod_action in subreddit.mod.log(action=action, mod=mod, limit=config.pull_limit):
        if mod_action.created_utc > ignore_before:
            if mod_action.details == details:
                # check for both flair edits AND 
                # try to put our removed threads back in our cache after a restart
                # the mod action just has a link to the actual target submission :U
                submission = reddit.submission(url=f"http://www.reddit.com{mod_action.target_permalink}")
                found_submission(submission)

def check_submission(config, submission):

    log.debug(f"Checking {submission.id}")
    age = datetime.now() - datetime.fromtimestamp(submission.created_utc)
    log.debug(f"Submission {submission.id} age: {age}")

    removed = False
    if hasattr(submission, "banned_by") and submission.banned_by is not None:
        removed = True
        if submission.banned_by != my_name:
            log.debug(f"Submission {submission.id} was removed by someone else ({submission.banned_by}), ignoring")
            return State.IGNORE

    if age >= config.warn_delay:
        log.debug(f"Submission {submission.id} old enough to be warned")

        warning_comment, warn_age = was_warned(submission)
        log.debug(f"Submission {submission.id} warned: {warning_comment!=False} when: {warn_age}")

        if not has_comment(submission):
            log.debug(f"Submission {submission.id} hasnt commented yet")
            # user hasnt commented yet

            if removed:
                # post removed and user hasnt commented, keep it removed (until they comment)
                log.debug(f"Submission {submission.id} already removed")
                return State.CHECK, config.slow_check_delay
            elif not warning_comment:
                log.debug(f"Submission {submission.id} sending warning")
                # has not been warned yet
                warn_submission(submission, config.warn_message)
                # recheck often for their comment
                return State.CHECK, None
            elif warn_age and \
                warn_age >= config.remove_delay:
                log.debug(f"Submission {submission.id} removing")
                # submitter was warned long enough ago and didnt do anything
                remove_submission(submission, warning_comment, config.remove_message)
                # check less often because they took so long
                return State.CHECK, config.slow_check_delay
            else:
                # post warned but not removed yet, they might comment soon
                return State.CHECK, None
        else:
            log.debug(f"Submission {submission.id} commented posted, approve and ignore.")
            # user has commented undo all our work
            approve_submission(submission, warning_comment, removed)
            # never check again
            return State.IGNORE, None
    else:
        # thread too young, check often for changes
        return State.CHECK, None
        
def has_comment(submission):
    submitter_name = submission.author.name
    for comment in submission.comments:
        if comment.author.name == submitter_name:
            return True
    return False

def was_warned(submission):
    for comment in submission.comments:
        if comment.author.name == my_name:
            age = datetime.now() - datetime.fromtimestamp(comment.created_utc)
            return comment, age
    return False, False

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

    reddit = connect(config)


    while True:
        try:
            monitor(reddit, config)
        except InsufficientScope as e:
            log.error(f"PRAW raised InsufficientScope! Make sure you have the following scopes: {','.join(REQUIRED_SCOPES)}")
            raise e
        except praw.exceptions.PRAWException:
            log.error(f"PRAW raised an exception! Logging but ignoring.", exc_info=True)
        time.sleep(5*60)
        # in the case of an error, sleep 5 minutes and try again