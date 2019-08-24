FROM python:3-alpine

WORKDIR /app
ADD ./subshowcasebot/bot.py /app
ADD ./requirements.txt /app

## Install any needed packages specified in requirements.txt
RUN pip install --trusted-host pypi.python.org -r requirements.txt

CMD [ "python", "bot.py" ]