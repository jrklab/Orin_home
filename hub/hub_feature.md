# Orin Home Hub
## Features
### Task 1
This device is a Jetson Orin NX 16GB device, it connects to an USB camera, and an USB speakerphone. It also connects to a Sono speaker via WiFi. The current hub stream live video from the camera, and can play audio (hold to speak) on the Sono speaker. Now I want to add the speakerphone function. when the web page is loaded, it will become a web conference call, with live stream of video from the USB camera, and live stream of two way audio on the USB speakerphone (USB speaker phone to web app speaker, and web app mic to USB speaker phone), like a conference call. disable the sono speaker function for now, but save the code, and it may be used later. 
### Task 2
I want to turn this web app into a mutiple page web app
1. page 1 is the designed conference call with both video stream and two way audio
2. page 2 is to play an audio message (hold to speak) on the Sono speaker
3. page 3 is reserved to control LeKiwi robot
4. page 4 is reserved for other functions, TBD
you may need to split the app.py into multiple files as needed to make the code more organized and readable. 
### Task 3
I want to add a feature, when Orin detects "Orin, Orin, call my dad" or "Orin, Orin, send my dad a message" from the speakerphone, Double Orin is required, and it will 
1. send a message to this discord webhook URL (https://discord.com/api/webhooks/1493519437477707816/6ijma81f5GVWslvCBhWFOSgxEhz_DKzk6lhYdTOX8QiCZU5VKZYxbcaUm2uzBAysXrrk)
2. include the message and any message after this wake up phrase
3. include a camera capture image
4. play an audio message at the speaker to indicate weather the message is sent successfully or not
make sure the camera and speakerphone is released, and handle any errors properly.

