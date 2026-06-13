**Weltrade MT5 Bot**

User Setup & Operations Guide

*Read this fully before starting your first session*

**Keep this document confidential. Do not share your credentials with
anyone.**

**1. How the Bot Works**

The Weltrade Bot is an automated trading system. Once running on your
VPS, it connects to your MetaTrader 5 platform and places trades on your
behalf based on the settings you configure. You control everything
through a simple web page that opens in any browser on any device.

**1.1 The Grid Bounce Strategy**

The bot uses a strategy called Grid Bounce. Here is what it does in
plain terms:

-   When you start the bot on an asset, it immediately opens two trades
    at the current price --- one Buy and one Sell. This is called the
    Centre Pair. The price at this moment is called the Centre.

-   You set a Grid Distance (in pips). The bot watches the price. When
    the price moves that distance away from the Centre, it closes one of
    the original trades and opens three new ones at the new price level.

-   The bot now bounces back and forth between two fixed price levels.
    Every time it bounces, it opens three new trades and closes one old
    one.

-   This continues until one trade closes on its own by hitting its
    profit target (TP) or stop-loss (SL). When that happens, the bot
    closes all remaining trades and immediately starts a fresh cycle
    from the current price.

-   The loop repeats until the session timer runs out or you manually
    stop the bot.

**1.2 Terms You Need to Know**

  -----------------------------------------------------------------------
  **Term**              **What it means**
  --------------------- -------------------------------------------------
  **Centre**            The price where the current cycle started. All
                        distances are measured from here.

  **Grid Distance**     How far (in pips) the price must move before the
                        bot opens new trades.

  **TP (Take Profit)**  The price where a trade closes automatically with
                        a profit.

  **SL (Stop Loss)**    The price where a trade closes automatically to
                        limit a loss.

  **Pair**              A matched Buy + Sell opened together at the same
                        price.

  **Single**            One unpaired trade in the direction the price is
                        moving.

  **Nuclear Reset**     All trades closed, new cycle starts from the
                        current price.

  **Lot Size**          The volume of a trade. Bigger lot = bigger profit
                        and bigger loss.

  **Max Positions**     Maximum trades in one cycle. Must be a multiple
                        of 3.

  **Set**               A group of lot size settings. Multiple sets
                        activate one after another.

  **Session Timer**     How long the bot is allowed to run before it
                        stops automatically.

  **Graceful Stop**     Bot finishes the current cycle then stops. No
                        trades closed early.

  **Terminate All**     All trades closed immediately. Use with caution.
  -----------------------------------------------------------------------

**2. Features**

**2.1 Multi-Asset Trading**

You can run the bot on multiple assets at the same time (e.g. FX Vol 20
and SFX Vol 99 simultaneously). Each asset runs its own independent
cycle with its own settings.

**2.2 Multiple Sets**

Each asset can be configured with multiple Sets. Set 1 trades first.
When it reaches its maximum positions, the bot automatically switches to
Set 2, then Set 3, and so on. Sets go in order and do not repeat. This
lets you use different lot sizes at different stages of a session.

**2.3 2nd Entry System**

When the bot opens a new triple of trades on a bounce, one of the three
is an unpaired directional trade. This trade gets its own separate TP
and SL settings under the 2nd Entry section --- separate from the paired
trades --- giving you finer control without affecting the rest.

**2.4 Automatic Lot Splitting**

Each asset has a broker-enforced maximum lot size per order. If your
configured lot size is higher than allowed, the bot silently splits it
into multiple smaller orders of the same type to reach your intended
total. You do not need to do anything.

**2.5 Volume Limit Warning**

As you type lot sizes in the settings panel, the bot simulates the
strategy and warns you if your configuration would exceed the total
volume limit for that asset. A red highlight appears on the problematic
field and a popup warning appears. You can dismiss it, but positions
beyond the limit may be missed by the broker.

**2.6 Volatility Tolerance**

A safety feature that automatically resets a cycle if the market makes
an abnormally large move. Choose the sensitivity in Global Settings:

-   Off: No protection.

-   1.5x: Resets if price moves 50% beyond your grid distance from the
    nearest active level.

-   1.75x / 2x / 2.25x / 2.5x: Increasingly tolerant thresholds (75%,
    100%, 125%, 150% overshoot).

*The calculation automatically accounts for the asset\'s current spread
to avoid false resets.*

**2.7 Auto-Save Configuration**

A toggle in Global Settings called Auto-save config. When on, any change
saves automatically about one second after you stop typing. When off,
click Update Configuration manually.

**2.8 Copy Lot Setup**

To reuse the same lot sizes and max positions across multiple assets,
click Copy Lot Setup above the asset list. Step 1: pick a source asset
(last edited is pre-selected). Step 2: pick target assets. The copy
saves automatically to the server.

**2.9 Session History**

Every session is logged. Open the Session History panel to view or
download logs showing every trade, every TP/SL hit, every reset, and all
configuration changes.

> **Important:** After your session timer expires, press Terminate All
> and confirm in the Live Terminal that all positions are closed before
> starting a new session. Skipping this may cause the bot to fail to
> start next time.

**3. Setting Up Your VPS**

The bot must run on a Windows VPS (Virtual Private Server) --- a Windows
computer in a data centre that stays on 24 hours a day. Your personal
laptop is not suitable because it would need to stay on and connected at
all times.

> **Note:** A VPS is a remote Windows computer you connect to from your
> own machine. Everything installed or running on the VPS stays running
> even after you disconnect.

**3.1 Minimum VPS Requirements**

  -----------------------------------------------------------------------
  **Requirement**       **Minimum Specification**
  --------------------- -------------------------------------------------
  Operating System      Windows Server 2019, Windows 10, or Windows 11
                        (64-bit) PLEASE MAKE SURE THE OPERATING SYSTEM IS WINDOWS

  RAM                   4 GB (preferably 8GB)

  Storage               60 GB SSD

  CPU                   2 cores

  Internet              Stable broadband, at least 10 Mbps

  Location              Choose a data centre close to your broker\'s
                        servers for lowest latency
  -----------------------------------------------------------------------

**3.2 Connecting to Your VPS**

1.  Press the Windows key on your computer, type Remote Desktop
    Connection, and open it.

2.  In the Computer field, enter your VPS IP address (provided by your
    VPS provider) and click Connect.

3.  Enter the username and password provided by your VPS provider.

4.  You are now inside your VPS. Everything you do here runs on the VPS,
    not your own computer.

**3.3 Installing Required Software**

Install these four things on your VPS in order.

**A --- Install Python**

5.  Open Microsoft Edge or Chrome on your VPS.

6.  Go to: https://www.python.org/downloads/release/python-3120/

7.  Click the yellow Download Python button, Please make sure it is PYTHON 3.12 as that was what the development phase was done with and run the installer.

8.  CRITICAL: On the first screen of the installer, tick Add Python to
    PATH before clicking Install Now.

9.  Wait for installation to finish and click Close.

**B --- Install Visual Studio Code**

10. Go to: https://code.visualstudio.com/

11. Click Download for Windows and run the installer.

12. Accept all default options during installation.

**C --- Install Weltrade MetaTrader 5**

13. Log in to your Weltrade account at https://www.weltrade.com

14. Download the MetaTrader 5 (MT5) terminal for Windows from their
    platform downloads section.

15. Run the installer and complete setup.

16. Open MT5 and log in with your Weltrade trading account credentials.

17. Leave MT5 open and running at all times while the bot is active.

> **Important:** The bot cannot place trades if MT5 is closed. MT5 must
> always be open on your VPS while the bot is running.

**D --- Install the Bot**

18. Copy or download your bot folder onto the VPS and extract it to a
    memorable location, e.g. C:\\weltrade-bot

19. Open Visual Studio Code, click File \> Open Folder, and select your
    bot folder.

20. Open the terminal: Terminal \> New Terminal in the menu bar.

21. In the terminal at the bottom, type the following command and press
    Enter:

> pip install -r requirements.txt

22. Wait for all packages to install. This may take a few minutes.

**4. Adding Your Credentials**

The bot needs your MT5 login details and Supabase credentials (provided
by your administrator) to operate. These are stored in a file called
.env inside the bot folder.

> **Important:** Never share your .env file with anyone. It contains
> your trading account password.

**4.1 Running the Setup Script**

23. Open File Explorer and go to your bot folder (e.g.
    C:\\weltrade-bot).

24. Double-click the file named setup_env.bat.

25. The script asks a series of questions. Type each answer and press
    Enter.

26. When asked for the MT5 terminal path, press Enter to accept the
    default (if you installed MT5 in its default location). Otherwise
    type the full path to terminal64.exe.

27. When finished, a .env file is created automatically in the bot
    folder.

    Note: If this step doesn't work please contact the admin as I would give you another guide on how to connect your broker account

**4.2 What Each Setting Means**

  -----------------------------------------------------------------------
  **Setting**           **What to enter**
  --------------------- -------------------------------------------------
  MT5_LOGIN             Your Weltrade trading account number (digits
                        only, e.g. 12345678)

  MT5_PASSWORD          Your Weltrade trading account password

  MT5_SERVER            Your broker server name. Find it in MT5 under
                        File \> Login to Trade Account. Looks like:
                        Weltrade-Live

  MT5_PATH              Full path to terminal64.exe. Default: C:\\Program
                        Files\\Weltrade MT5\\terminal64.exe

  SUPABASE_URL          Provided by your administrator. Starts with
                        https://

  SUPABASE_KEY          Provided by your administrator. A long string of
                        letters and numbers.

  BOT_PORT              Port number the bot web page runs on. Default:
                        800
  -----------------------------------------------------------------------

**5. Opening the Bot Port**

Windows has a built-in firewall that blocks outside connections by
default. You need to open one port so you can access the bot web page
from any browser.

> **Note:** You only need to do this once. The port stays open until you
> change it.

**5.1 Running the Port Setup Script**

28. Open File Explorer and go to your bot folder.

29. Right-click the file named open_port.bat and select Run as
    administrator.

30. Click Yes when Windows asks for permission.

31. The script reads your port from the .env file automatically and
    opens it in the firewall.

32. When finished, the script displays your bot address, for example:

> http://45.144.242.97:800

33. Save this address. This is what you type into any browser to open
    the bot.

    Note: If this step i.e. the custom script I have written fails for some reason or just doesn't work, again contact me and I would tell you an alternative method. 

> **Important:** If your VPS provider has a separate firewall or
> Security Groups control panel (common with providers like Vultr,
> Hetzner, DigitalOcean, or AWS), you may also need to open the port
> there. Look for a section called Firewall, Security Groups, or Inbound
> Rules and add a TCP rule for your chosen port (default: 800).

**6. Starting the Bot**

Once your VPS is set up, Python is installed, MT5 is open and logged in,
and your .env file is configured, you are ready to start.

**6.1 First Start**

34. Open Visual Studio Code on your VPS.

35. Click File \> Open Folder and open your bot folder.

36. Open the terminal: Terminal \> New Terminal.

37. Type the following and press Enter:

> python main.py

38. The bot will start. You will see output like this in the terminal:

> \[SERVER\] Starting: Launching Monolith Engine\...
>
> \[OK\] MT5 connected successfully
>
> Uvicorn running on http://0.0.0.0:800

39. Open any browser and go to your bot address (shown in the terminal).
    Log in with your email and password.

> **Note:** Your bot address is shown every time the bot starts. It is
> always your VPS IP address followed by a colon and your port number.
>
> **Tip:** Keep the VS Code terminal window open while the bot is
> running. Closing it will stop the bot.

**6.2 Keeping the Bot Running**

-   Do not close the VS Code terminal window while the bot is running.

-   When disconnecting from Remote Desktop, use the Disconnect button
    --- do not click Shut down or Sign out.

-   To restart the bot after a VPS reboot, open VS Code, open the
    terminal, and run python main.py again.

**7. Using the Bot**

**7.1 Configuring Your Settings**

40. In the Settings panel on the left, tick the assets you want to
    trade.

41. A settings panel expands for each selected asset. Fill in Grid
    Distance, TP, SL, lot sizes, and Max Positions.

42. Set Max Time (minutes) in the Global Settings section. The bot will
    gracefully stop after this time.

43. Click Update Configuration to save, or turn on Auto-save config so
    changes save automatically.

**7.2 Controls**

  -----------------------------------------------------------------------
  **Button**            **What it does**
  --------------------- -------------------------------------------------
  Start All             Starts the bot on all enabled assets
                        simultaneously.

  Start (per asset)     Starts the bot on one specific asset only.

  Stop All              Graceful stop --- completes the current cycle
                        then stops.

  Terminate All         Closes all trades immediately. Use carefully.

  Start Bot (top        Same as Start All.
  button)               
  -----------------------------------------------------------------------

**7.3 Reading the Dashboard**

  -----------------------------------------------------------------------
  **Display**           **What it shows**
  --------------------- -------------------------------------------------
  Live Price            Current price of the first active asset.

  Positions             Current open positions / maximum allowed.

  Session               Elapsed time / total session time / time
                        remaining.

  Bot State             Idle, Active (cycle number), Resetting, or
                        Stopping.

  Live Terminal         Real-time log of everything the bot is doing.
  -----------------------------------------------------------------------

**8. Troubleshooting**

  -----------------------------------------------------------------------
  **Problem**           **Solution**
  --------------------- -------------------------------------------------
  Bot page does not     Check that python main.py is still running in the
  load in browser       VS Code terminal. Check you are using the correct
                        IP address and port number.

  MT5 connection failed Make sure MT5 is open and you are logged in.
  at startup            Check MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, and
                        MT5_PATH in your .env file.

  Bot does not start    Press Terminate All and confirm all positions are
  after a previous      closed in the Live Terminal, then restart with
  session               python main.py.

  Positions not opening The asset may have reached max positions. Check
                        the Positions counter on the dashboard and review
                        the Live Terminal for error messages.

  Page shows Offline    Wait 5 seconds and refresh. If it persists, check
  even though the bot   the terminal for error messages.
  is running            

  Forgot the bot web    Run open_port.bat again --- it will display your
  address               address. Or check your VPS IP in your VPS
                        provider\'s control panel.

  Red highlight on a    Your lot configuration for that asset would
  lot size field        exceed the broker\'s volume limit. Reduce the
                        flagged lot size until the red highlight
                        disappears.

  Cycle resets          Volatility Tolerance may have triggered due to an
  unexpectedly          abnormal market move. This is a safety feature.
                        Check the Live Terminal for a VOLATILITY RESET
                        message.
  -----------------------------------------------------------------------

**9. Quick Reference**

**Before Every Session**

-   MT5 is open and logged in on the VPS.

-   python main.py is running in the VS Code terminal.

-   Bot web page loads at your address.

-   Settings are configured and saved.

-   Session timer is set.

**Ending a Session Cleanly**

-   Wait for the timer to expire, or click Stop All for a graceful stop.

-   Click Terminate All and confirm in the Live Terminal that all
    positions are closed.

-   You may close the browser. Leave the bot process running for the
    next session.

**Emergency --- Close Everything Immediately**

-   Click Terminate All on the bot web page.

-   If the page is unreachable, open MT5 directly and close all
    positions manually from within the terminal.

**Your Bot Address**

> **http://**\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_\_**:**\_\_\_\_\_\_\_\_

*Write your VPS IP address and port number in the spaces above.*