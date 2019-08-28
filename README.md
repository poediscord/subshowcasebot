Showcase Bot
===

A bot to help manage our rule about showcases

HOW TO
===
Build your docker container
```
docker build -t subshowcasebot:latest .
```

Upload a secret with your config
```
cat instance/config.json | docker secret create subshowcaseconfig.json -
```

Create the service
```
docker service create --name subshowcasetest --secret subshowcaseconfig.json -e CONFIG_FILE='/run/secrets/subshowcaseconfig.json' subshowcasebot:latest
```


boom done
