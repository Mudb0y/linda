/*
 * A Swing window with one plain JTextArea named Notes, for tests/linux/test_javaEditor.py: Backspace in
 * it says the character it deleted, as in a JetBrains IDE's editor, which reaches AT-SPI through the same
 * Java ATK wrapper.
 *
 * Run inside the rig only, with java launching this source file, the wrapper's jar on the class path,
 * its library on java.library.path and javax.accessibility.assistive_technologies naming it. Its
 * argument, if any, is the text the area starts with. It prints "ready" once it is shown and quits when
 * its standard input closes.
 */

import java.io.IOException;
import javax.swing.JFrame;
import javax.swing.JTextArea;
import javax.swing.SwingUtilities;

public class JavaEditor {
	public static void main(String[] args) {
		String text = args.length > 0 ? args[0] : "";
		SwingUtilities.invokeLater(() -> show(text));
		Thread quit = new Thread(() -> {
			try {
				while (System.in.read() != -1) {
				}
			} catch (IOException e) {
			}
			System.exit(0);
		});
		quit.setDaemon(true);
		quit.start();
	}

	static void show(String text) {
		JFrame frame = new JFrame("Java editor");
		frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
		JTextArea area = new JTextArea(text);
		area.getAccessibleContext().setAccessibleName("Notes");
		frame.add(area);
		frame.setSize(400, 300);
		frame.setLocation(0, 0);
		frame.setVisible(true);
		area.requestFocusInWindow();
		System.out.println("ready");
		System.out.flush();
	}
}
