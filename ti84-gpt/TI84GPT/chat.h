// Conversation memory plus building/reading OpenAI "chat completions" JSON.
// (Works with any service that speaks the same API.)
#pragma once
#include <stddef.h>

#include <string>
#include <vector>

namespace chat {

class Conversation {
 public:
  void clear() { turns_.clear(); }
  bool empty() const { return turns_.empty(); }
  void addUser(const std::string &text);
  void addAssistant(const std::string &text);
  // Forget the newest turn (used when a request fails, so a retry does not
  // send the question twice).
  void dropLast();
  // Forget the oldest turns until the history is at most maxChars long.
  void trim(size_t maxChars);

  // The request body for POST {api base}/chat/completions.
  std::string requestJson(const std::string &model, const std::string &systemPrompt) const;

 private:
  struct Turn {
    bool user;
    std::string text;
  };
  std::vector<Turn> turns_;
};

// Reads a chat-completions response. Returns true with the assistant's text,
// or false with a human-readable reason.
bool parseResponse(const std::string &body, std::string &content, std::string &error);

// Reads the ids out of a GET {api base}/models response.
void parseModelList(const std::string &body, std::vector<std::string> &ids);

}  // namespace chat
