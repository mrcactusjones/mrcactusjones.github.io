#include "chat.h"

#include "json_mini.h"

namespace chat {

void Conversation::addUser(const std::string &text) { turns_.push_back(Turn{true, text}); }

void Conversation::addAssistant(const std::string &text) { turns_.push_back(Turn{false, text}); }

void Conversation::dropLast() {
  if (!turns_.empty()) turns_.pop_back();
}

void Conversation::trim(size_t maxChars) {
  for (;;) {
    size_t total = 0;
    for (size_t i = 0; i < turns_.size(); i++) total += turns_[i].text.size();
    if (total <= maxChars || turns_.size() <= 1) return;
    turns_.erase(turns_.begin());
    // Keep the history starting with a user turn.
    if (turns_.size() > 1 && !turns_.front().user) turns_.erase(turns_.begin());
  }
}

std::string Conversation::requestJson(const std::string &model, const std::string &systemPrompt) const {
  std::string body = "{\"model\":\"" + json::escape(model) + "\",\"messages\":[";
  bool first = true;
  if (!systemPrompt.empty()) {
    body += "{\"role\":\"system\",\"content\":\"" + json::escape(systemPrompt) + "\"}";
    first = false;
  }
  for (size_t i = 0; i < turns_.size(); i++) {
    if (!first) body += ",";
    first = false;
    body += "{\"role\":\"";
    body += turns_[i].user ? "user" : "assistant";
    body += "\",\"content\":\"" + json::escape(turns_[i].text) + "\"}";
  }
  body += "]}";
  return body;
}

bool parseResponse(const std::string &body, std::string &content, std::string &error) {
  content.clear();
  error.clear();
  const char *begin = body.c_str();
  const char *end = begin + body.size();
  const char *root = json::skipSpace(begin, end);
  if (!root || root >= end || *root != '{') {
    error = "Reply was not JSON";
    return false;
  }

  // {"error": {"message": "..."}}  or  {"error": "..."}
  const char *err = json::member(root, end, "error");
  if (err && err < end && *err != 'n') {
    if (*err == '{') {
      const char *message = json::member(err, end, "message");
      if (!json::readString(message, end, error)) error = "API error";
    } else if (!json::readString(err, end, error)) {
      error = "API error";
    }
    return false;
  }

  const char *choices = json::member(root, end, "choices");
  const char *choice = json::element(choices, end, 0);
  const char *message = json::member(choice, end, "message");
  const char *text = json::member(message, end, "content");
  if (text && text < end && *text == '"') {
    if (json::readString(text, end, content) && !content.empty()) return true;
  } else if (text && text < end && *text == '[') {
    // Some services return a list of parts: [{"type":"text","text":"..."}]
    for (int i = 0;; i++) {
      const char *part = json::element(text, end, i);
      if (!part) break;
      std::string piece;
      if (json::readString(json::member(part, end, "text"), end, piece)) content += piece;
    }
    if (!content.empty()) return true;
  }

  const char *refusal = json::member(message, end, "refusal");
  if (json::readString(refusal, end, content) && !content.empty()) return true;

  error = "No answer in the reply";
  return false;
}

void parseModelList(const std::string &body, std::vector<std::string> &ids) {
  ids.clear();
  const char *begin = body.c_str();
  const char *end = begin + body.size();
  const char *data = json::member(json::skipSpace(begin, end), end, "data");
  for (int i = 0;; i++) {
    const char *item = json::element(data, end, i);
    if (!item) break;
    std::string id;
    if (json::readString(json::member(item, end, "id"), end, id)) ids.push_back(id);
  }
}

}  // namespace chat
