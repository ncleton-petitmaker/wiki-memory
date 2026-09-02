# ADR 0002 — Authorization precedes retrieval

Status: accepted.

Every event, evidence relationship, assertion, and search document carries scope/ACL. Team narrows candidates by authorized spaces and applies exact ACL before returning search results or blob bytes. Transformations intersect evidence ACLs and cannot widen visibility automatically.

Consequences: no post-answer redaction, no vector/lexical side-channel by unauthorized content, and lower recall where evidence permissions disagree—which is the safe failure mode.
