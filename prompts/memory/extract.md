Identify only durable information the user explicitly asked to remember or information that is clearly useful across future sessions and safe to retain.

Return JSON:
{
  "memories": [
    {
      "type": "preference|fact|task|context",
      "content": "...",
      "confidence": 0.0,
      "explicitly_requested": false,
      "expires_in_days": null
    }
  ]
}

Do not store passwords, authentication tokens, payment details, medical information, or entire conversation transcripts. Do not convert model assumptions into memories.

