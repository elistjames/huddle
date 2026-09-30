# Ai Collab Chat (PRD)

> **Source of truth.** Mirrored from Notion (last edited 2026-09-29): https://www.notion.so/eb17d720d67f82968716817f4a66fc54
> Product name: **Huddle**. Parent Notion page: "AI Group Chat App".

## Purpose

A place for users to communicate with other users and AI simultaneously and synchronously. Today user chats and AI chats are separate: people who chat with an AI have their own context and conversations. This app merges the two by bringing users together in a shared-context chat, for seamless AI-assisted team collaboration.

## Overview & Objectives

Ship a lightweight chat application that hosts a real-time chat session with 2 or more users and chat bots. Users can create sessions, add other users, and add AI models as virtual chat participants. The core functional requirement is that users chat with users, just as they normally would, but with an autonomous AI agent included. The agent is not a prompted model: it sits in the conversation and adds real thoughts, when it feels like it, just like a human would.

**Objectives**

- First build the real-time collab messaging service.
- Then incorporate a single AI agent loop, system-prompted to sit in a conversation and add thoughts only when necessary, not after every user message.
- Multi-user and multi-agent chat groups over a shared context window (later).

## Problem Statement

See Purpose. (The Notion page has a placeholder for a Product Discovery Doc link; none exists yet.)

## Technical Scope

**In scope (v1)**

- On app startup there is a single chat session; users who run the app join the same session.
- Sending a message shows to other users in the chat almost instantly.
- Multiple users chatting in that one session, with the AI agent.
- AI agent pre-added to the session, actively absorbing the conversation, and writing and sending a message when it decides it should.
- After a long lull (default 20 min, configurable 10 to 30), the agent may post one re-engagement message, and only if the conversation still feels open.
- The agent runs on free local models (Llama via Ollama), so there is no model spend.
- The agent's usage is capped by a per-session token budget that users can see, including when it refills ("Huddle AI is resting · back at 3:40 PM").
- Three-checkmark receipts: stored, delivered to at least one other person, and read by the AI (the AI check is amber).

**Out of scope**

- Typing indicators for humans (the agent's "is writing" state stays in scope).
- User and agent mentions (@).
- Multiple saved chat sessions (follow-up).
- Push or email notifications (nudges only reach people who have the app open).
- Multi-agent chats.

## System and Designs

Design lives in the Huddle Design System (see `design/` in this repo): the brand (Huddle, ink + amber, Inter), the logo, the components and a clickable prototype. It replaces the Figma placeholder. There is no Figma file for v1.

## Open Questions & Risks

| Risk / question | Mitigation |
|---|---|
| On what conditions will the agent send a message or keep listening? | Needs a tuned system prompt and agent loop orchestration. Policy is defined in Deep Dive §5: debounce, cooldown, 1/N participation share, no two agent messages in a row, and one re-engagement nudge per lull when the conversation is still open. |
| The agent could overload the local machine in a busy session. | Per-session token bucket that users can see. When empty, the agent rests and shows when it will be back. |
| Small local models are weaker at judging when to speak than hosted frontier models. | Tune the decider against the evaluations in Deep Dive §11. Hosted models stay a later option if quality falls short. |
| The shared context window can fill up fast as more users message. | Context compaction: keep recent chats verbatim, summarize the oldest 80%. |
| The agent could send very long messages, more frequently, for a chat app. | Limit message size in the system prompt. |

## Success Metrics

- **Primary:** in the average chat session, each AI agent should account for a share of the total daily messages equal to 1 / (number of participants in the chat, including agents).
  - Example: 2 users and one agent, after a day of chat, 1/3 of the messages should be written by the AI.
  - This shows the AI contributes without over-contributing; each user has an equal opportunity to share a thought.
  - If the per-agent ratio is consistently below target, the agent is not contributing much.
- **Secondary:** average messages per session in the first day after creation. High means the chat was engaging; low means it went stale.
