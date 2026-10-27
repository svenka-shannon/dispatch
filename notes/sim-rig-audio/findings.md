# Sim rig + office audio research — verified 2026-09-18

Eric's gear: Trak Racer TR160 V5, GT fibreglass seat, Simagic Alpha EVO 12Nm,
Simagic P1000 pedals, RTX 3070 PC. Sims: AC, ACC, iRacing. Pacifica CA.
(NB: the base/pedals are SIMAGIC, not Moza — I briefed the agents wrong initially.)

## HAPTICS
- ButtKicker effectively UNBUYABLE: all 36 SKUs out of stock on thebuttkicker.com,
  sold out at Advanced SimRacing + Sim Motion. ETAs slipping to Q4 2026.
  Guitammer (parent) exited Ch.11 in 2022. Don't plan around it.
- 4-corner shaker setups are WASTED on a rigid 160x40 rig. Community consensus:
  "you're just wasting power and making a mush of vibration". One 4-corner owner:
  "I can't tell you left from right... I've unplugged them all."
  Corner separation only works if you spring-isolate (Slip-Angle, $650).
- Buy 2: one under seat pan, one under pedal plate. Not 4.
- Dayton BST-1 $54.98 @ Parts Express (was $99.99). 4-pack $199.98.
- AMP MATTERS MORE THAN SHAKER COUNT. Nobsound is widely reported to fail
  ("mine failed after a week and a half", "I went through 3").
  Better: Aiyima A07 Max ~$70+PSU (2ch), Douk Audio M4 $79.99 (4ch).
  Target ~100W/ch into 4ohm. One shaker per channel; BST-1 is 4ohm.
- BEST SINGLE UPGRADE given a 12Nm DD: Simagic P-HPR pedal haptic, $49 + $25 PSU.
  Native to his P1000s. Brake/ABS feel is the one thing a DD wheel cannot give.
  "It's not a buzz, it's practically someone hitting the pedal with a mallet."
- SimHub is still the standard (v9.12.8 released 2026-09-17). License is
  pay-what-you-want EUR 8-30 and is EFFECTIVELY MANDATORY: unlicensed runs
  shakers at 10 FPS vs 60 FPS. Motion add-on (EUR 34.99) NOT needed.
  New free tool: ShakeFitter profile generator.
- Mounting: DON'T DRILL the fibreglass seat. 3M Dual Lock tape first.
  Trak Racer TR-SHAPLATE3 under-seat mount $89 (4.8*, 84 reviews, fits BST-1).
  Isolate with Shore A85 rubber standoffs (A55 too soft). Zero metal-to-metal.
- Gotchas: shakers loosen rig bolts -> blue Loctite 242, re-torque as maintenance.
  Structure-borne vibration travels through floors; neighbours complain about
  SHAKING not noise, so turning it down doesn't help. Run at ~40%, less is more.
- Entry build ~$310-375. Mid (4 shakers) ~$490-530.

## RIG SPEAKERS
- Headphones win for actual racing; speakers win for immersion. Most people
  who own both: speakers for hotlapping, headphones for races.
- GT fibreglass seat BLOCKS side/rear sound and smears imaging -> 5.1 on a rig
  is a trap. Buy good stereo, not surround.
- DON'T mount speakers on the rig if running shakers: the frame transmits
  20-80Hz into the speaker cabinet -> rattle + muddy mids. Use desk/stands.
- Trak Racer 5-speaker mount TR-SPMT-TR8020 $59 — OUT OF STOCK until early
  Oct 2026, Amazon listing dead. AND it's sized for Logitech-class satellites,
  NOT 10lb studio monitors. Skip.
- JBL 305P MkII $133ea/$266pr (Thomann US, in stock). Best value.
- Kali LP-6 V2 ~$378-498/pr — 6.5", boundary-compensation DIP switches.
- Genelec 8030C ~$1750/pr if going high.
- Headphones: Sennheiser HD 560S ~$200 open-back (best soundstage/PRTF) or
  Beyerdynamic DT 770 Pro X ~$200 closed.
- SimHub REQUIRES A SEPARATE AUDIO DEVICE for shakers — game audio and ShakeIt
  must not share one, or they bleed into each other. Behringer UMC404HD $92
  (4 out) + ~$100 class-D amp. Set channel count in BOTH Windows AND the driver.
- Skip a sub for the RIG — shakers own the ".1" for racing.

## OFFICE SYSTEM
- *** NEVER route the PC/rig through Sonos. *** Verified Sonos community answer
  (Apr 2026): minimum 30ms latency on HDMI eARC, 75ms on Line-In. Deliberate
  buffering, architectural, not a setting. 75ms = ~9 frames at 120fps, like
  sitting 80ft from your own car. Fine for music, disqualifying for driving.
- Recommended ~$1,320-1,400:
  Kali LP-6 V2 pair $498 (Sweetwater, in stock)
  + SVS SB-1000 Pro sealed 12" sub $699 (also seen $599.99; SVS direct = 45-day trial)
  + IsoAcoustics ISO-155 stands ~$120
- Furniture-look alternative: KEF Q3 Meta pair $899.99 + WiiM Amp Ultra $349
  (Amazon street price, MSRP $529 — has HDMI ARC, sub out, AirPlay 2, RoomFit
  room correction, phono, real volume knob).
- SUB vs SHAKERS is NOT either/or: shakers = tactile, no room bass, racing only.
  Sub = air, music. He wants music, so the sub is justified on music grounds.
  Crossover ~70-80Hz with 6.5" monitors. Do the "subwoofer crawl" for placement.
- BIGGEST per-dollar gain is ROOM, not gear: tweeters at ear height aimed at
  your ears, equilateral triangle, speakers off the desk surface, and TWO
  absorption panels at first reflection points (mirror trick). GIK 242 ~$130ea.
- SKIP: Amazon foam tiles (absorb only >1kHz, make rooms worse), DACs over ~$200,
  cables, power conditioners, REL (brand premium).
- If he wants house-wide Sonos parity in the office, add ONE Era 100 (~$249)
  for background music only, wired system for everything else.

## SOURCES
Parts Express, thebuttkicker.com, Sweetwater, Thomann US, us.kef.com, SVS,
simagic.com, trakracer.com, simhubdash.com, SimHub wiki,
Sonos community thread 6933718 (the latency numbers).
