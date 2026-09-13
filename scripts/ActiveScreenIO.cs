using System;
using System.IO;
using System.Text;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.ComponentModel;

public static class ActiveScreenIO {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool MoveFileExW(string source, string target, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool ReplaceFileW(string target, string source, IntPtr backup, uint flags, IntPtr exclude, IntPtr reserved);
    [DllImport("kernel32.dll")] static extern uint GetConsoleOutputCP();

    public static void Publish(string target, string json) {
        string name="Local\\FulfillFlowHeartbeat"+Convert.ToHexString(
            System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(Path.GetFullPath(target).ToLowerInvariant()))).ToLowerInvariant();
        using(var mutex=new System.Threading.Mutex(false,name)) {
            bool held=false;
            try {
                try { held=mutex.WaitOne(250); }
                catch(System.Threading.AbandonedMutexException) {
                    held=true;
                    throw new Win32Exception(735,"Heartbeat producer mutex was abandoned");
                }
                if(!held) throw new TimeoutException("Heartbeat publication mutex exceeded 250 ms");
                PublishLocked(target,json);
            } finally { if(held) mutex.ReleaseMutex(); }
        }
    }
    static void PublishLocked(string target, string json) {
        // One producer. Never delete the destination before replacing its directory entry.
        string next=target+".next";
        using(var f=new FileStream(next,FileMode.Create,FileAccess.Write,FileShare.None)) {
            byte[] bytes=new UTF8Encoding(false,true).GetBytes(json);
            f.Write(bytes,0,bytes.Length); f.Flush();
        }
        bool replaced=File.Exists(target)
            ? ReplaceFileW(target,next,IntPtr.Zero,0,IntPtr.Zero,IntPtr.Zero)
            : MoveFileExW(next,target,0);
        if(!replaced) throw new Win32Exception(Marshal.GetLastWin32Error());
    }

    public sealed class Capture {
        public int ExitCode; public uint CodePageBefore, CodePageAfter;
        public byte[] Stdout, Stderr; public string Text, ErrorText;
        public string DecodeError;
    }
    public static Capture Power(string argument) {
        if(argument!="/query" && argument!="/requests") throw new ArgumentException("read-only power query required");
        var result=new Capture { CodePageBefore=GetConsoleOutputCP() };
        using(var process=new Process())
        using(var stdout=new MemoryStream())
        using(var stderr=new MemoryStream()) {
            process.StartInfo=new ProcessStartInfo(Path.Combine(Environment.SystemDirectory,"powercfg.exe"),argument) {
                UseShellExecute=false, RedirectStandardOutput=true, RedirectStandardError=true
            };
            process.Start();
            var output=process.StandardOutput.BaseStream.CopyToAsync(stdout);
            var error=process.StandardError.BaseStream.CopyToAsync(stderr);
            if(!process.WaitForExit(30000)) {
                process.Kill();
                if(!process.WaitForExit(5000)) throw new TimeoutException("Owned powercfg process did not exit");
                throw new TimeoutException("Read-only powercfg exceeded 30 seconds");
            }
            if(!System.Threading.Tasks.Task.WaitAll(new[]{output,error},5000)) throw new TimeoutException("Power output drain exceeded 5 seconds");
            result.ExitCode=process.ExitCode;
            result.Stdout=stdout.ToArray(); result.Stderr=stderr.ToArray();
        }
        result.CodePageAfter=GetConsoleOutputCP();
        try {
            if(result.CodePageBefore==0 || result.CodePageBefore!=result.CodePageAfter)
                throw new InvalidOperationException("Console code page unavailable or changed during capture");
            var encoding=Encoding.GetEncoding((int)result.CodePageBefore,EncoderFallback.ExceptionFallback,DecoderFallback.ExceptionFallback);
            result.Text=encoding.GetString(result.Stdout).Replace("\r\n","\n");
            result.ErrorText=encoding.GetString(result.Stderr).Replace("\r\n","\n");
        } catch(Exception ex) { result.DecodeError=ex.GetType().Name; }
        return result;
    }
}
