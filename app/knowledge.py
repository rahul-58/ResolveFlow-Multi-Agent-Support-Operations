"""Local knowledge-base and policy search."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class KnowledgeBase:
    def __init__(self, knowledge_dir: Path, policies_dir: Path, max_hits: int = 5):
        self.knowledge_dir = knowledge_dir
        self.policies_dir = policies_dir
        self.max_hits = max_hits

    def _documents(self, directory: Path) -> list[tuple[str, str]]:
        if not directory.exists():
            return []
        return [
            (str(path.relative_to(directory)), path.read_text(encoding="utf-8", errors="replace"))
            for path in sorted(directory.rglob("*.md"))
        ]

    @staticmethod
    def _tokens(text: str) -> set[str]:
        stopwords = {
            "the", "and", "for", "one", "two", "can", "you", "this", "that", "with",
            "from", "have", "has", "been", "are", "was", "were", "will", "our", "your",
            "not", "but", "after", "before", "into", "about", "they", "their", "there",
        }
        return {
            token.lower()
            for token in re.findall(r"[a-zA-Z0-9]+", text)
            if len(token) > 2 and token.lower() not in stopwords
        }

    def search(self, query: str, include_policies: bool = False) -> list[dict[str, Any]]:
        documents = self._documents(self.knowledge_dir)
        if include_policies:
            documents += self._documents(self.policies_dir)
        query_tokens = self._tokens(query)
        ranked: list[tuple[int, dict[str, Any]]] = []
        for name, text in documents:
            score = len(query_tokens & self._tokens(text))
            if score:
                ranked.append(
                    (
                        score,
                        {
                            "source": name,
                            "score": score,
                            "text": text[:12000],
                        },
                    )
                )
        ranked.sort(key=lambda item: (-item[0], item[1]["source"]))
        strong = [item for score, item in ranked if score >= 2]
        candidates = strong or [item for _, item in ranked]
        return candidates[: self.max_hits]

    def policy_text(self) -> str:
        return "\n\n".join(text for _, text in self._documents(self.policies_dir))

    def format_hits(self, hits: list[dict[str, Any]]) -> str:
        if not hits:
            return "No local knowledge-base document matched the query."
        return "\n\n".join(
            f"SOURCE: {hit['source']}\n{hit['text']}" for hit in hits
        )

    @staticmethod
    def _sources(source_text: str) -> list[str]:
        return re.findall(r"^SOURCE:\s*(\S+)", source_text, flags=re.MULTILINE)

    def verify_response(self, response: str, source_text: str) -> dict[str, Any]:
        lower = response.lower()
        policy = self.policy_text().lower()
        issues: list[str] = []
        warnings: list[str] = []
        source_lower = source_text.lower()
        sources = self._sources(source_text)
        prohibited = {
            "password": "The response must not request or expose passwords.",
            "cvv": "The response must not request CVV or full payment credentials.",
            "full card number": "The response must not request a full card number.",
            "authentication code": "The response must not request an authentication code.",
        }
        for phrase, message in prohibited.items():
            if phrase == "password":
                # Mentioning a password-reset flow is safe; reject only requests
                # to disclose or enter the secret itself.
                secret_request = re.search(
                    r"\b(?:send|share|provide|tell|give|enter|submit|type|disclose|reveal|confirm)\b"
                    r"[^.?!]{0,40}\b(?:your\s+)?password\b",
                    lower,
                )
                if secret_request and not re.search(
                    r"\b(?:do not|don't|never|not|avoid)\b",
                    lower[max(0, secret_request.start() - 30) : secret_request.start()],
                ):
                    issues.append(message)
                continue
            if phrase in lower and not re.search(
                rf"\b(?:never|do not|don't|not|no|avoid)\b[^.?!]{{0,80}}"
                rf"\b{re.escape(phrase)}s?\b",
                lower,
            ):
                issues.append(message)
        if "guarantee" in lower or "guaranteed" in lower:
            issues.append("The response makes a guarantee that requires confirmed evidence.")
        if re.search(
            r"\b(?:function call|tool call|json for|to best answer the given prompt|based on the tool call)\b"
            r"|\{\s*\"name\"\s*:\s*\"[^\"]+\"\s*,\s*\"parameters\"",
            lower,
        ) or re.search(
            r"\b(?:get_ticket|get_ticket_history|search_knowledge_base|get_support_policy|list_open_tasks)\b",
            lower,
        ):
            issues.append("The response contains internal tool instructions or model reasoning.")
        if re.search(
            r"\b(?:retrieved sources?|what these sources support|relevant sources|"
            r"searched local documentation|based on the provided information|"
            r"i\s+(?:removed|deleted|rewrote|revised)|\(\s*note\s*:)",
            lower,
        ):
            issues.append("The response contains internal research narration rather than customer guidance.")

        commitment = r"\b(?:i|we)(?:['’]ll| will| can| would| am going to| are going to)\b"
        def unsafe_commitment(target: str, window: int = 140) -> bool:
            pattern = rf"{commitment}[^.?!]{{0,{window}}}\b(?:{target})\b"
            return any(
                not re.search(
                    r"\b(?:not|never|no|cannot|can't|unable|won't|wouldn't)\b",
                    match.group(0),
                )
                for match in re.finditer(pattern, lower)
            )

        if unsafe_commitment("refund|reverse|reimburse|credit|chargeback|charge back"):
            issues.append(
                "The response promises or initiates a financial remedy before the billing outcome is confirmed."
            )
        if unsafe_commitment("correct|fix|prevent|stop"):
            issues.append(
                "The response promises a system change or prevention outcome that the support workflow cannot confirm."
            )
        if unsafe_commitment(
            "keep you updated|update you|provide a solution|provide an update|provide you with an update|give you an update",
            window=120,
        ):
            issues.append("The response promises future communication or resolution without an approved workflow.")
        if unsafe_commitment("follow up|keep you posted|get back to you|contact you", window=100):
            issues.append("The response promises future customer follow-up before an approved workflow exists.")
        internal_action = re.search(
            r"\b(?:task|ticket|investigation|escalation)\b[^.?!]{0,100}"
            r"\b(?:will|shall|is going to|are going to)\b[^.?!]{0,60}"
            r"\b(?:be\s+)?(?:created|opened|assigned|escalated)\b",
            lower,
        )
        if unsafe_commitment("escalate", window=120) or (
            unsafe_commitment("create|created|open|opened|assign|assigned", window=120)
            and re.search(r"\b(?:task|ticket|investigation|escalation)\b", lower)
        ) or internal_action or re.search(
            r"\b(?:we|the support team|the billing team|the account team|the shipping team)\s+"
            r"should\s+(?:create|open|assign|escalate)\b[^.?!]{0,80}"
            r"\b(?:task|ticket|investigation|escalation)\b",
            lower,
        ):
            issues.append("The response promises an internal action before human approval.")
        if re.search(
            r"\b(?:create|creating|open|opening|assign|assigning)\b[^.?!]{0,100}"
            r"\b(?:task|ticket|investigation|escalation)\b",
            lower,
        ):
            issues.append("The response promises an internal action before human approval.")
        if re.search(
            r"\brefunds?\b[^.?!]{0,80}\b(?:will|would|can|may)\b[^.?!]{0,50}"
            r"\b(?:be\s+)?(?:issued|processed|provided|sent)\b",
            lower,
        ):
            issues.append("The response promises a financial remedy before the billing outcome is confirmed.")
        if re.search(
            r"\bduplicate charges?\b[^.?!]{0,80}\b(?:are|is)\s+(?:not allowed|not supposed to occur|prohibited|not permitted|illegal)\b",
            lower,
        ) and "not allowed" not in source_lower and "prohibited" not in source_lower:
            issues.append("The response makes a policy claim that is not supported by the retrieved source.")
        if re.search(
            r"\b(?:checked|verified|confirmed)\b[^.?!]{0,80}\b(?:system|account|charge|transaction)\b",
            lower,
        ):
            issues.append("The response claims a system or transaction check that was not supported by the available tools.")
        if re.search(
            r"\b(?:appears|indeed|confirmed)\b[^.?!]{0,80}\b(?:mistake|error|wrong|resolved|refunded)\b",
            lower,
        ):
            issues.append("The response states an outcome that was not confirmed by the available evidence.")
        if re.search(
            r"\b(?:confirmed|verified|determined)\b[^.?!]{0,100}"
            r"\b(?:charged twice|duplicate charge|duplicate charges|pending|settled)\b",
            lower,
        ):
            issues.append("The response presents a customer report as a confirmed transaction outcome.")
        if re.search(
            r"\b(?:entitled to|qualif(?:y|ied) for|will receive)\b[^.?!]{0,100}"
            r"\b(?:refund|replacement|reimbursement|credit)\b",
            lower,
        ):
            issues.append("The response promises a refund or replacement without approved supporting evidence.")
        if re.search(
            r"\b(?:contact|call|reach out to)\b[^.?!]{0,60}\b(?:carrier|shipping company)\b"
            r"|\bprovide you with an update on the status\b",
            lower,
        ):
            issues.append("The response promises an unsupported carrier investigation or status update.")

        if not source_text.strip():
            issues.append("No supporting knowledge text was retrieved; the response cannot be approved as grounded.")
        elif not sources:
            issues.append("The retrieved knowledge does not contain a canonical SOURCE marker.")
        response_sources = re.findall(r"\bsource:\s*([a-zA-Z0-9_.-]+)", lower)
        if source_text.strip() and (
            not response_sources
            or not any(
                response_source.lower() in {source.lower() for source in sources}
                for response_source in response_sources
            )
        ):
            issues.append("The response must identify at least one retrieved supporting source.")
        if "human" in lower and "human" not in policy:
            warnings.append("Review human-escalation wording against the policy file.")
        return {
            "approved": not issues,
            "issues": issues,
            "warnings": warnings,
            "sources": sources,
        }

    @staticmethod
    def verify_action_plan(action_plan: str) -> list[str]:
        """Reject action plans that present proposed work as completed work."""

        lower = action_plan.lower()
        if not lower.strip():
            return []
        if re.search(
            r"\b(?:function call|tool call|json for|based on the tool call|"
            r"get_ticket|get_ticket_history|search_knowledge_base|get_support_policy|"
            r"list_open_tasks)\b"
            r"|\{\s*\"name\"\s*:\s*\"[^\"]+\"\s*,\s*\"parameters\"",
            lower,
        ) or re.search(
            r"\b(?:i|we)\s+(?:will|can|am going to|are going to)\s+"
            r"(?:call|retrieve|search|check)\b",
            lower,
        ):
            return ["The action plan contains internal tool instructions or execution claims."]
        if re.search(
            r"\b(?:already|previous agent|resolved|processed|informed|completed|closed|has been|was)\b"
            r"[^.?!]{0,100}\b(?:refund|resolution|customer|charge|issue|task)\b",
            lower,
        ):
            return [
                "The action plan claims a completed outcome; it may only propose work supported by current evidence."
            ]
        if re.search(
            r"\b(?:based on|given)\b[^.?!]{0,100}\b(?:customer(?:'s|s)? response|initial response|new information)\b"
            r"|\b(?:it seems|it appears)\b[^.?!]{0,100}\b(?:customer|they)\b"
            r"|\b(?:customer|they)\b[^.?!]{0,80}\b(?:checked|confirmed|verified|provided)\b",
            lower,
        ):
            return [
                "The action plan infers customer information that was not present in the ticket."
            ]
        if re.search(
            r"\b(?:customer|they)\b[^.?!]{0,80}\b(?:has not|hasn't|did not|didn't)\s+respond(?:ed)?\b"
            r"|\bno further action is required\b",
            lower,
        ):
            return [
                "The action plan makes an unsupported claim about customer follow-up or next steps."
            ]
        if re.search(
            r"\b(?:based on the retrieved|based on the knowledge base|according to the policy)\b"
            r"[^.?!]{0,100}\b(?:customer|their response|initial response)\b",
            lower,
        ):
            return [
                "The action plan contains unsupported reasoning about the customer's state."
            ]
        return []
