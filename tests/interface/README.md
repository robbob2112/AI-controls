# HAL interface tests

On the isolated Linux testbed, stage the candidate renderer and run:

```sh
node tests/interface/renderer.test.mjs
python3 tests/interface/smoke.py
```

The renderer suite parses the complete inline JavaScript and runs the disclosure regression. Run it with Node 20 or later. The smoke test expects the lab chat listener at `127.0.0.1:18443` and the deterministic chat fixture. It creates and removes its own conversation. It uses Python 3 and the standard library.

Add a regression for every interface change in this directory. Reproduce the original failure where practical, and run the regression and smoke test against the tested revision before merge. UI behavior that depends on the browser also needs a browser check against the Linux testbed. Keep production credentials and live user data out of fixtures.

Record the candidate commit, actual commands and outcomes in the change review. Publish tested commits to GitHub and verify the private Gitea pull mirror has the same refs before using it for deployment.
