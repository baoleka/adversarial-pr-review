# Severity

Severity describes the harm if the change ships as is, for realistic use. It is not a measure of
how sure you are; evidence and verification handle that.

| Severity | Meaning | Examples |
|---|---|---|
| critical | Data loss or corruption, a security hole an outsider can reach, or a crash or wrong result on the main path for common input. | A command-line option silently drops its value for every user; a file is truncated when a write fails; a shell command is built from a file name. |
| major | Wrong behavior for realistic input, a regression of documented or tested behavior, a broken public contract, or an error that is swallowed so callers can't see it. | An off-by-one in a range bound; an exception that used to propagate is now caught and ignored; a default value changed without a changelog entry. |
| minor | Wrong behavior only for unusual input, a missing test for a new branch, misleading naming or messages, or a maintainability problem a reviewer would ask to fix. | A message that reports the wrong limit; a test that never asserts the new branch. |
| nit | Style or wording only. At most 3 per review, and never about anything a linter or formatter catches. | A clearer variable name. |

Blocking: critical and major findings block approval once verified. Minor and nit findings are
listed for the author and don't block, with one exception: in a later round of the fix loop, a new
behavior defect (correctness, regression, behavior change, contract, error handling, concurrency,
resource or security) on lines the author's fix added is a regression of that fix, and it blocks at
any severity once verified.
