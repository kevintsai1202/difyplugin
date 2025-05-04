# Line Bot Plugin Integration Guide
Author: @kevintsai1202 (https://github.com/kevintsai1202/difyplugin.git)
Version: 0.0.5
Type: extension

## What's Changed
# 0.0.5
  1. Fix/New Feature: When group_id (LINE group chat), room_id (chat room) exist, Session uses group_id or room_id to save conversation_id. The effect is that group chats on Dify will maintain the same conversation. thanks to [@ryantsai](https://github.com/ryantsai]
  2. New Feature: When the message contains markdown, send to LINE as FlexMessage. Can be adjusted in Plugin parameters, default is off. thanks to [@ryantsai](https://github.com/ryantsai]
  3. New Feature: Image upload uses Dify API URL parameterization, supports community edition scenarios. thanks to [@ryantsai](https://github.com/ryantsai]

# 0.0.4
  1. Add the /clearconversationhistory command, thanks to [@ryantsai](https://github.com/ryantsai]
    When a user enters it on LINE, it will clear the session storage, and the next message sent to Dify will start a brand-new, clean session.
  2. Image message handling, thanks to [@ryantsai](https://github.com/ryantsai]
    Generate responses based on the uploaded images.
  3. Message Handling Enhancement, thanks to [@ryantsai](https://github.com/ryantsai]
    Added support for Markdown image URLs. `[url](http://xxx.xxx)`
  4. Handle image content in responses[@ryantsai](https://github.com/ryantsai]
    Ensure that generated images are properly displayed on LINE.
  5. Identifier Retrieval Support, thanks to [@jimcchi](https://github.com/jimcchi]
    Capture user_id, room_id, and group_id from incoming messages.

## Plugin Overview
The Line Bot plugin integrates the Dify chat workflow application with the Line Official Account Messaging API. It enables users to interact with AI through a Line Official Account. The plugin only processes message reception and responses; it does not store any user information.

## Setup Steps
Follow these steps to install and configure the Line Bot plugin:
1. Create a Provider and Messaging API Channel
  Go to the [LINE Developers](https://developers.line.biz) website。 
  <img src="./_assets/2025-03-10 20 34 06.png" width="600" />

2. Copy the Channel Secret and Channel Access Token of the Messaging API
  - Navigate to the Basic settings page.
  - Copy the Channel Secret (if it hasn’t been generated yet, click "Issue" to create one).
  <img src="./_assets/2025-03-10 21 07 14.png" width="600" />
  - Navigate to the Messaging API page.
  - Enable Use Webhook.
  - Copy the Channel Access Token (if it hasn’t been generated yet, click "Issue" to create one).
  <img src="./_assets/2025-03-10 21 06 36.png" width="600" /> 

3. Set Up the Dify Line Bot Endpoint
  - Set an Endpoint Name.
  - Paste the Channel Secret and Channel Access Token.
  - Obtain the API Key for the Dify workflow.(option)
  - Set the image variable, and ensure that the workflow also configures the same file variable.
  - Select a Chat Workflow. 
  <img src="./_assets/440135816-e30b5969-f51f-4afa-aaeb-e79cf5e2f197.png" width="600" />
  <img src="./_assets/2025-04-29 16 33 49.png" width="600" />
  <img src="./_assets/2025-04-29 16 36 50.png" width="600" />
  <img src="./_assets/2025-04-29 16 37 05.png" width="600" />

4. Save and Copy the Line Bot Webhook URL
  <img src="./_assets/2025-03-10 21 02 33.png" width="600" />

5. Set the Webhook URL and Verify
  - Return to the Messaging API page on the LINE Developers platform.
  - Paste the Webhook URL obtained in the previous step.
  - Verify the Line Bot. 
  <img src="./_assets/2025-03-10 21 03 50.png" width="600" /> 
  <img src="./_assets/2025-03-10 21 08 36.png" width="600" />
6. Use Line to add the Line Official Account and start chatting with AI.
  <img src="./_assets/S__320659478.jpg" width="600" />

  /clearconversationhistory
  <img src="./_assets/2025-04-29 16 52 05.png" width="600" />

  Describe the image
  <img src="./_assets/2025-04-29 16 55 41.png" width="600" />

  Generation image
  Sample ChatFlow on Dify using gpt-image-1 for generation(DSL file can be imported into Dify)
  <img src="./_assets/437889599-42f6c682-3705-4e83-b00b-bcee18a5ec59.png" width="600" />

  <a href="https://github.com/user-attachments/files/19927343/LinebotImageChatSample.zip">LinebotImageChatSample.zip</a>

  FlexMessage
   <img src="./_assets/440135990-6a4e05b3-9679-445e-bbc6-9acfd413a026.png" width="600" />