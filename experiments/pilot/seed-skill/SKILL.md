---
name: uppercase-file
description: Convert an input text file to uppercase.
---
Read /input/input.txt, convert ASCII lowercase letters to uppercase, and write /output/result.txt.

## Procedure
Use tr '[:lower:]' '[:upper:]' on the input and save the result. Preserve line endings.
