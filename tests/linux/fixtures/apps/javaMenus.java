/*
 * A Swing window with a menu bar, a submenu and a popup menu, as a JetBrains IDE has them, for
 * tests/linux/test_javaMenus.py. Swing keeps the keyboard focus on the window's root pane while a menu
 * is open, and the Java ATK wrapper says which item is highlighted with a focus of its own.
 *
 * Run inside the rig only, with java launching this source file, the wrapper's jar on the class path,
 * its library on java.library.path and javax.accessibility.assistive_technologies naming it. Its
 * argument "heavyweight" puts every popup in a window of its own, as menus too big for the window
 * are, and a further "popupItems" gives the Recent submenu's items its popup menu as their parent,
 * as a JetBrains IDE's submenus have it. It prints "ready" once it is shown and quits when its standard input closes.
 */

import java.awt.Component;
import java.awt.event.KeyEvent;
import java.io.IOException;
import java.util.Arrays;
import javax.accessibility.Accessible;
import javax.accessibility.AccessibleContext;
import javax.swing.AbstractAction;
import javax.swing.JButton;
import javax.swing.JComponent;
import javax.swing.JFrame;
import javax.swing.JMenu;
import javax.swing.JMenuBar;
import javax.swing.JMenuItem;
import javax.swing.JPanel;
import javax.swing.JPopupMenu;
import javax.swing.KeyStroke;
import javax.swing.SwingUtilities;

public class JavaMenus {
	static boolean popupItems;

	public static void main(String[] args) {
		if (args.length > 0 && args[0].equals("heavyweight")) {
			JPopupMenu.setDefaultLightWeightPopupEnabled(false);
		}
		popupItems = Arrays.asList(args).contains("popupItems");
		SwingUtilities.invokeLater(JavaMenus::show);
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

	static JMenu menu(String name, Object... items) {
		JMenu menu = new JMenu(name);
		for (Object item : items) {
			menu.add(item instanceof JMenuItem ? (JMenuItem) item : new JMenuItem((String) item));
		}
		return menu;
	}

	/**
	 * A submenu whose items give its popup menu as their accessible parent, as a JetBrains IDE's
	 * submenus do: Swing's own menus make themselves their items' parent as they are asked for them.
	 */
	static JMenu popupItemsMenu(String name, String... items) {
		JMenu menu = new JMenu(name) {
			@Override
			public AccessibleContext getAccessibleContext() {
				if (accessibleContext == null) {
					accessibleContext = new AccessibleJMenu() {
						@Override
						public Accessible getAccessibleChild(int index) {
							Component child = getPopupMenu().getComponent(index);
							return child instanceof Accessible ? (Accessible) child : null;
						}
					};
				}
				return accessibleContext;
			}
		};
		for (String item : items) {
			menu.add(new JMenuItem(item));
		}
		return menu;
	}

	static void show() {
		JFrame frame = new JFrame("Java menus");
		frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
		JMenuBar bar = new JMenuBar();
		JMenu recent = popupItems
			? popupItemsMenu("Recent", "First project", "Second project")
			: menu("Recent", "First project", "Second project");
		bar.add(menu("File", "New", "Open", recent, "Quit"));
		bar.add(menu("Edit", "Cut", "Copy"));
		frame.setJMenuBar(bar);

		JButton button = new JButton("Press me");
		JPopupMenu popup = new JPopupMenu();
		for (String name : new String[] {"Alpha", "Beta", "Gamma"}) {
			popup.add(new JMenuItem(name));
		}
		button.setComponentPopupMenu(popup);
		// Swing opens a component's popup with the pointer alone; a keyboard user opens it with the
		// keys GTK and Windows use.
		AbstractAction showPopup = new AbstractAction() {
			@Override
			public void actionPerformed(java.awt.event.ActionEvent e) {
				popup.show(button, 0, button.getHeight());
			}
		};
		button.getInputMap(JComponent.WHEN_FOCUSED).put(KeyStroke.getKeyStroke("shift F10"), "showPopup");
		button.getInputMap(JComponent.WHEN_FOCUSED)
			.put(KeyStroke.getKeyStroke(KeyEvent.VK_CONTEXT_MENU, 0), "showPopup");
		button.getActionMap().put("showPopup", showPopup);

		JPanel panel = new JPanel();
		panel.add(button);
		frame.add(panel);
		frame.setSize(400, 300);
		// Away from the pointer, which the rig leaves in the middle of the screen: a heavyweight popup
		// opened under it would highlight the item it lands on.
		frame.setLocation(0, 0);
		frame.setVisible(true);
		button.requestFocusInWindow();
		System.out.println("ready");
		System.out.flush();
	}
}
