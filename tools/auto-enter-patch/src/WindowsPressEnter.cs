using System;
using System.Runtime.InteropServices;
using System.Threading;

namespace OpenWhisprAutoEnter
{
    internal static class Program
    {
        private const int INPUT_KEYBOARD = 1;
        private const uint KEYEVENTF_KEYUP = 0x0002;
        private const ushort VK_RETURN = 0x0D;
        private const uint MAPVK_VK_TO_VSC = 0;

        private static readonly int[] ModifierVks =
        {
            0xA2, // VK_LCONTROL
            0xA3, // VK_RCONTROL
            0xA0, // VK_LSHIFT
            0xA1, // VK_RSHIFT
            0xA4, // VK_LMENU (Left Alt)
            0xA5, // VK_RMENU (Right Alt)
            0x5B, // VK_LWIN
            0x5C, // VK_RWIN
        };

        [StructLayout(LayoutKind.Sequential)]
        private struct INPUT
        {
            public int type;
            public InputUnion u;
        }

        [StructLayout(LayoutKind.Explicit)]
        private struct InputUnion
        {
            [FieldOffset(0)]
            public MOUSEINPUT mi;
            [FieldOffset(0)]
            public KEYBDINPUT ki;
            [FieldOffset(0)]
            public HARDWAREINPUT hi;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MOUSEINPUT
        {
            public int dx;
            public int dy;
            public uint mouseData;
            public uint dwFlags;
            public uint time;
            public IntPtr dwExtraInfo;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct KEYBDINPUT
        {
            public ushort wVk;
            public ushort wScan;
            public uint dwFlags;
            public uint time;
            public IntPtr dwExtraInfo;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct HARDWAREINPUT
        {
            public uint uMsg;
            public ushort wParamL;
            public ushort wParamH;
        }

        [DllImport("user32.dll", SetLastError = true)]
        private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

        [DllImport("user32.dll")]
        private static extern short GetAsyncKeyState(int vKey);

        [DllImport("user32.dll")]
        private static extern uint MapVirtualKey(uint uCode, uint uMapType);

        private static bool IsAnyModifierDown()
        {
            for (int i = 0; i < ModifierVks.Length; i++)
            {
                if ((GetAsyncKeyState(ModifierVks[i]) & 0x8000) != 0)
                {
                    return true;
                }
            }
            return false;
        }

        private static void WaitForModifiersRelease(int maxWaitMs)
        {
            int waited = 0;
            while (waited < maxWaitMs && IsAnyModifierDown())
            {
                Thread.Sleep(10);
                waited += 10;
            }
        }

        private static int Main(string[] args)
        {
            bool dryRun = false;
            if (args != null)
            {
                foreach (string arg in args)
                {
                    if (string.Equals(arg, "--test-dry-run", StringComparison.OrdinalIgnoreCase))
                    {
                        dryRun = true;
                    }
                }
            }

            WaitForModifiersRelease(400);

            if (dryRun)
            {
                Console.WriteLine("DRY_RUN_OK");
                return 0;
            }

            ushort scan = (ushort)MapVirtualKey(VK_RETURN, MAPVK_VK_TO_VSC);
            INPUT[] inputs = new INPUT[2];

            inputs[0].type = INPUT_KEYBOARD;
            inputs[0].u.ki.wVk = VK_RETURN;
            inputs[0].u.ki.wScan = scan;
            inputs[0].u.ki.dwFlags = 0;

            inputs[1].type = INPUT_KEYBOARD;
            inputs[1].u.ki.wVk = VK_RETURN;
            inputs[1].u.ki.wScan = scan;
            inputs[1].u.ki.dwFlags = KEYEVENTF_KEYUP;

            uint sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
            if (sent != (uint)inputs.Length)
            {
                int err = Marshal.GetLastWin32Error();
                Console.Error.WriteLine("ERROR: SendInput failed (sent=" + sent + ", err=" + err + ")");
                return 1;
            }

            Console.WriteLine("ENTER_OK");
            return 0;
        }
    }
}
