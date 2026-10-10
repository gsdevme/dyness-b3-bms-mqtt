# Changelog

## 0.1.0 (2026-10-10)


### Features

* **alerts:** debounced hysteresis alerts with flag roll-ups ([f76acd3](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/f76acd3590d863c3c50ad53fc3fdaaa771ab9952))
* **app:** main loop wiring, signals, exit codes and publish cadence ([4c286b9](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/4c286b9d04637404a7b83d7bad67a07b8da123c2))
* **canbus:** python-can transport, link-state query and simulated stack ([d4ced40](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/d4ced409d5576dce92ff1eff09a3ff0d5525efd0))
* **config:** parse and validate environment settings ([3234ca9](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/3234ca98734fb804d8a243291cbcf5161d675434))
* **discovery:** device-based Home Assistant discovery builder ([eb08853](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/eb08853fac10e5bce90109b67d8e513a7d3f633e))
* **health:** pure health state machine and recovery state store ([482d19f](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/482d19fd6d1c9c40f16e1d619779f1a2a11829de))
* **install:** Makefile install, uninstall and operational targets ([998c1a0](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/998c1a093889b4efdd4464621c9db40cb581f5d4))
* **localog:** daily CSV history with gzip and retention housekeeping ([576b208](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/576b208d6fb6cc9d31b5503985e925e39c7e8d0d))
* **mqtt:** paho publisher, topics and state payload builders ([d423a1b](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/d423a1be39a9e384b4a55b8a430a8fdfd75eb6c8))
* **packaging:** systemd unit, udev rule, env example and root helpers ([eef82db](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/eef82dbce68822c2fb044e038a35c05daf904547))
* **protocol:** classify and decode B3 frames with guarded poll builder ([1beaf80](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/1beaf806df21cb17454787cc776d602ff0e30efc))
* **sdnotify:** dependency-free sd_notify with watchdog cadence ([9463c44](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/9463c44317f8a9d79fa16424f976bcbda030f810))
* **telemetry:** latest-value store with freshness and derived stack values ([c0426c5](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/c0426c5c28cc5ea3eafdf80cf488eac7ea294290))


### Bug Fixes

* **canbus:** ignore python-can config files and environment ([eee3e3e](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/eee3e3ed9f42a6540b69601a882ed8f2d667312c))
* **mqtt:** log failed broker connects and queue-full drops ([9a8ccb7](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/9a8ccb7f6ba87847e7c1a5bdc190458e5c785dec))


### Performance Improvements

* **localog:** cheaper, bounded gzip housekeeping ([61d1e47](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/61d1e471951253dd439abb63cf18d4aced8a9f3a))


### Documentation

* **install:** install from the GitHub release archive ([627ebf9](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/627ebf96dc9da6733319df53114c52da10777302))
* **specs:** add living specifications and initial design ([920539a](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/920539aebad9accbb7234a971a353e9d5f183a01))
* **specs:** align protocol, alert and CI wording for implementation ([2a7a4f7](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/2a7a4f719d776d912359f5341f45a9171bbde821))
* **specs:** move final-review follow-ups out of the deviations record ([8bea3a6](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/8bea3a62fe10b02a775568ebc1d303f6202f0d24))
* **specs:** record implementation deviations found in review ([65c07e7](https://github.com/gsdevme/dyness-b3-bms-mqtt/commit/65c07e70958a9c4ec8276c375641fc0cb037e486))
