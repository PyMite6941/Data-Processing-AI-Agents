Hello! This is my International AI Agents 2026 hackathon project.

This project was a learning process that I very much enjoyed so any support [like following the repo] is much appreciated.

Check out the license before use, this is my first MIT license in my account!

## Features

This project only has three features, which make this whole project work.

- The Backend allows the AI Agents to always be accessible wherever you are in the world and that leads to the computation being universally available to everyone, and this can help mitigate the digital divide.

- The REST API allows me and others to use the API to use this bot's brain on other projects so that these AI Agents can be deployed in any project whenever needed.

- The Frontend allows an interface that can be used by anyone browsing the internet that navigates to the website, allowing for easy uploading of files to the agents and then seeing the processes that the agents are working on and how they process since the verbose mode is on.

## Inspiration

Despite using computers as a computation mechanism, it never occured to me to try and utilize the recent computational developments to solve bigger issues, primarily data related. My dad uses AI all the time [sadly he chooses copilot, the corst choice he could make!] to get the analysis of the data he is looking at. This project isn't just for him, however. This project taught me how to use agents outside of the AI apps that I use such as OpenSwarm and Claude Code, helping me understand better how these apps work and developing my skills.

## What it does

This AI Agent project takes in data from the frontend [a react vite site hosted on (vercel here)[https://data-processing-ai-agents.vercel.app/]] and pushes it to the agent-run backend on hugging face that processes the request by breaking down the context into what the user wants and what the user gave context about. Then it goes to the agent that creates the custom prompt for the data analyst agent, which then the data analyst looks at the data and then analyzes the data by looking at outliers and summarizing the data points in regards to the context. Then the output agent formats the response from the data analyst into how the data should be represented for the use case provided and then rates the response given by the data analyst compared to what the user requested, allowing the user to see if the agent believes the question was properly answered based on their prompt.

## How we built it

I used react vite JS for the frontend, a REST API connecting the frontend to the backend through the use of async functions, and CrewAI as the backend agent manager and creator. The frontend is hosted on vercel and the backend is hosted on HuggingFace. I used any LLM API that I could get my hands on for free.

## Challenges we ran into

There were many challenges in this process including trying to run the frontend properly, LLM models kept returning errors despite me using the OpenRouter API properly, it was very difficult to make work. Also the testing phase took a while to load since the AI Agents thought deeply which is good however it made my job a bit long

## Accomplishments that we're proud of

This is a great project considering I'm currently studying for physics and precalculus finals for school as of submitting this project, so I hope that my studying pays off as much as I know this project will. I am also proud of the fact that this project didn't turn out a complete failure, it was quite nice to build and even use for processing my own data.

## What we learned

I learned about CrewAI and I realized how much of a game changer it really is for me to use, it creates agents and manages the agentic workflow flawlessly except for my own errors and poor prompting that get thrown in.

## What's next for Data Analyst

I have been working on making this Data Analyst project used in all of my major GitHub repos that have the ability to utilize AI in this way, ranging from clustering to explaining growth. I plan to use this as the backend in my next competition PhysTech 2026 as the processing force however I am not taking everything and copy-pasting it.
