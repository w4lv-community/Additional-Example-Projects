### GLA Summit 2025 cRIO Real-Time Demonstration

This complete distributed application was demonstrated at GLA Summit 2025, showcasing Workers running on both a CompactRIO real-time target and Windows host PC, featuring FPGA data acquisition (NI 9205 module at 10kHz), TCP/IP streaming, and live visualization.


### What You'll Learn:

- Building distributed Workers applications across RT targets and host PCs
- Integrating FPGA data with Workers for high-speed data acquisition
- Using TCP Server and Client Workers for data streaming between host-to-RT

**Complete working example demonstrating Workers architecture on real-time embedded systems.**

Learn more here: https://community.workersforlabview.io/articles/post/workers-for-labview-on-ni-real-time-targets-xXewjoaf40HUsPs

---

### Instructions:

1. Run the Windows Host PC application first. Make sure that you press the **Start Listening on Port** button on the front panel of the applications' Main UI, so that the cRIO applicaiton will be able to connect to the Host PC application.

2. On the Launcher VI of the cRIO application, make sure you include the IP address of the Host PC.

3. Run the cRIO Launcher VI and the two applications should connect. You will see the data from the cRIO streamed to the Host PC application.

4. Safely shutdown the cRIO application remotely through the Workers Debug Server's Application Manager
