// Temporary native power request and fail-closed observations; no persistent settings.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Threading;

public sealed class ActiveScreenGuard : IDisposable {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct WndClass {
        public uint style; public WndProc proc; public int extraClass, extraWindow;
        public IntPtr instance, icon, cursor, background;
        public string menu, name;
    }
    [StructLayout(LayoutKind.Sequential)] struct Msg {
        public IntPtr hwnd; public uint message; public UIntPtr wParam; public IntPtr lParam;
        public uint time; public int x, y; public uint privateValue;
    }
    [StructLayout(LayoutKind.Sequential)] struct PowerStatus {
        public byte ac, battery, percent, saver; public uint life, full;
    }
    delegate IntPtr WndProc(IntPtr h, uint m, UIntPtr w, IntPtr l);
    [DllImport("kernel32.dll")] static extern uint SetThreadExecutionState(uint f);
    [DllImport("kernel32.dll")] static extern bool GetSystemPowerStatus(out PowerStatus p);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern ushort RegisterClassW(ref WndClass c);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateWindowExW(uint e,string c,string n,uint s,int x,int y,int w,int h,IntPtr parent,IntPtr menu,IntPtr inst,IntPtr param);
    [DllImport("user32.dll")] static extern IntPtr DefWindowProcW(IntPtr h,uint m,UIntPtr w,IntPtr l);
    [DllImport("user32.dll")] static extern bool PeekMessageW(out Msg m,IntPtr h,uint a,uint b,uint f);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessageW(ref Msg m);
    [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr RegisterPowerSettingNotification(IntPtr h,ref Guid g,uint f);
    [DllImport("user32.dll")] static extern bool UnregisterPowerSettingNotification(IntPtr h);
    [DllImport("wtsapi32.dll")] static extern bool WTSRegisterSessionNotification(IntPtr h,uint f);
    [DllImport("wtsapi32.dll")] static extern bool WTSUnRegisterSessionNotification(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr OpenInputDesktop(uint f,bool inherit,uint access);
    [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr h);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern bool GetUserObjectInformationW(IntPtr h,int index,System.Text.StringBuilder b,int length,out int needed);
    readonly Thread worker; readonly WndProc callback; readonly List<string> events = new List<string>();
    volatile bool stop; public volatile bool Ready, Released; public volatile string Failure = "";
    public volatile int Display = -1; public long HeartbeatTicks;
    readonly Guid displayGuid = new Guid("6fe69556-704a-47a0-8f24-c28d936fda47");
    public ActiveScreenGuard() { callback=OnMessage; worker=new Thread(Run); worker.IsBackground=true; worker.Start(); }
    void Fail(string reason) { if(Failure=="") { Failure=reason; Record(reason); } }
    void Record(string text) { lock(events) events.Add(DateTime.UtcNow.ToString("o")+" "+text); }
    public string[] Events() { lock(events) return events.ToArray(); }
    IntPtr OnMessage(IntPtr h,uint m,UIntPtr w,IntPtr l) {
        if(m==0x218 && w.ToUInt64()==0x8013) {
            Guid g=(Guid)Marshal.PtrToStructure(l,typeof(Guid));
            if(g==displayGuid && Marshal.ReadInt32(l,16)==4) {
                Display=Marshal.ReadInt32(l,20); Record("display="+Display);
                if(Display!=1) Fail("display_not_on");
            }
        }
        if(m==0x218 && w.ToUInt64()==4) Fail("suspend_notification");
        if(m==0x2b1 && w.ToUInt64()!=8) Fail("session_transition_"+w.ToUInt64());
        return DefWindowProcW(h,m,w,l);
    }
    void Run() {
        IntPtr window=IntPtr.Zero, registration=IntPtr.Zero;
        try {
            string name="FulfillFlowPower"+Guid.NewGuid().ToString("N");
            WndClass c=new WndClass {proc=callback,name=name};
            if(RegisterClassW(ref c)==0) { Fail("window_class_failed"); return; }
            // Invisible top-level notification window, never shown or focused.
            window=CreateWindowExW(0,name,"",0,0,0,0,0,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero);
            if(window==IntPtr.Zero) { Fail("notification_window_failed"); return; }
            Guid g=displayGuid; registration=RegisterPowerSettingNotification(window,ref g,0);
            if(registration==IntPtr.Zero || !WTSRegisterSessionNotification(window,0)) { Fail("notification_registration_failed"); return; }
            if(SetThreadExecutionState(0x80000003)==0) { Fail("power_request_failed"); return; }
            Record("power_request_active"); DateTime start=DateTime.UtcNow;
            while(!stop) {
                Msg msg; while(PeekMessageW(out msg,IntPtr.Zero,0,0,1)) DispatchMessageW(ref msg);
                PowerStatus status;
                if(!GetSystemPowerStatus(out status) || status.ac!=1) Fail("ac_unconfirmed");
                IntPtr desktop=OpenInputDesktop(0,false,1);
                if(desktop==IntPtr.Zero) Fail("interactive_desktop_unavailable");
                else { try { int size; var b=new System.Text.StringBuilder(128);
                    if(!GetUserObjectInformationW(desktop,2,b,256,out size) || b.ToString()!="Default") Fail("session_not_unlocked");
                } finally { CloseDesktop(desktop); } }
                if(Display==-1 && (DateTime.UtcNow-start).TotalSeconds>5) Fail("display_unconfirmed");
                Ready=Display==1 && Failure=="";
                long now=DateTime.UtcNow.Ticks;
                if(HeartbeatTicks!=0 && now-HeartbeatTicks>TimeSpan.FromSeconds(3).Ticks) Fail("monitor_gap");
                Interlocked.Exchange(ref HeartbeatTicks,now);
                Thread.Sleep(250);
            }
        } catch { Fail("native_monitor_exception"); }
        finally {
            Released=SetThreadExecutionState(0x80000000)!=0;
            Record(Released ? "power_request_released" : "power_release_failed");
            if(registration!=IntPtr.Zero) UnregisterPowerSettingNotification(registration);
            if(window!=IntPtr.Zero) { WTSUnRegisterSessionNotification(window); DestroyWindow(window); }
        }
    }
    public void Dispose() { stop=true; if(!worker.Join(5000)) Fail("monitor_shutdown_timeout"); }
}
