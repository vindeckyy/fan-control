PREFIX ?= /usr/local
DESTDIR ?=
LIBDIR = $(DESTDIR)$(PREFIX)/lib/fan-control
BINDIR = $(DESTDIR)$(PREFIX)/bin
SHAREDIR = $(DESTDIR)$(PREFIX)/share/fan-control
APPDIR = $(DESTDIR)$(PREFIX)/share/applications
ICONDIR = $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps
METAINFODIR = $(DESTDIR)$(PREFIX)/share/metainfo
UNITDIR = $(DESTDIR)/etc/systemd/system
POLICYDIR = $(DESTDIR)$(PREFIX)/share/polkit-1/actions

.PHONY: all ui test lint clean install uninstall windows-exe

all: ui

ui:
	cd ui && npm ci && npm run build

MINGW_CC ?= x86_64-w64-mingw32-gcc
MINGW_WINDRES ?= x86_64-w64-mingw32-windres

windows-exe: packaging/icons/fan-control.ico
	$(MINGW_WINDRES) -I . -i packaging/windows/launcher.rc -o packaging/windows/launcher.res.o
	$(MINGW_CC) -O2 -s -municode -DGUI_APP=1 -mwindows packaging/windows/launcher.c packaging/windows/launcher.res.o -lshlwapi -o fan-control.exe
	$(MINGW_CC) -O2 -s -municode -DGUI_APP=1 -mwindows packaging/windows/launcher.c packaging/windows/launcher.res.o -lshlwapi -o fan-gui.exe
	$(MINGW_CC) -O2 -s -municode -DGUI_APP=0 -mconsole packaging/windows/launcher.c packaging/windows/launcher.res.o -lshlwapi -o fan-ctl.exe
	$(MINGW_CC) -O2 -s -municode -DGUI_APP=0 -mconsole packaging/windows/launcher.c packaging/windows/launcher.res.o -lshlwapi -o fan-daemon.exe

packaging/icons/fan-control.ico: packaging/icons/fan-control.svg
	magick -background none packaging/icons/fan-control.svg -define icon:auto-resize=256,128,64,48,32,16 packaging/icons/fan-control.ico

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
	cd ui && npm test
lint:
	ruff check .
clean:
	rm -rf ui/dist packaging/windows/launcher.res.o

install: ui
	install -d $(BINDIR) $(LIBDIR) $(SHAREDIR)/ui $(APPDIR) $(ICONDIR) $(METAINFODIR) $(POLICYDIR)
	install -Dm755 fan-daemon.py $(BINDIR)/fan-daemon
	install -Dm755 fan-gui.py $(BINDIR)/fan-gui
	install -Dm755 fan-ctl.py $(BINDIR)/fan-ctl
	install -m644 fan_backend.py fan_policy.py fan_runtime.py fan_controller.py fan_gtk.py fan_windows_gui.py fan_rpc.py fan_diagnostics.py fan_rules.py fan_engine.py fan_history.py $(LIBDIR)/
	install -Dm755 prepare-ec.sh $(LIBDIR)/prepare-ec.sh
	cp -a ui/dist/. $(SHAREDIR)/ui/
	install -Dm644 packaging/fan-control.desktop $(APPDIR)/fan-control.desktop
	install -Dm644 packaging/icons/fan-control.svg $(ICONDIR)/fan-control.svg
	install -Dm644 packaging/org.community.FanControl.metainfo.xml $(METAINFODIR)/org.community.FanControl.metainfo.xml
	install -Dm644 packaging/org.community.FanControl.policy $(POLICYDIR)/org.community.FanControl.policy
	sed 's|/usr/local/bin/fan-daemon|$(PREFIX)/bin/fan-daemon|' fan-daemon.service > $(LIBDIR)/fan-daemon.service
	install -Dm644 $(LIBDIR)/fan-daemon.service $(UNITDIR)/fan-daemon.service
	rm $(LIBDIR)/fan-daemon.service
	install -Dm644 packaging/tmpfiles.conf $(DESTDIR)/usr/lib/tmpfiles.d/fan-control.conf
	install -Dm644 packaging/sysusers.conf $(DESTDIR)/usr/lib/sysusers.d/fan-control.conf

uninstall:
	rm -f $(BINDIR)/fan-daemon $(BINDIR)/fan-gui $(BINDIR)/fan-ctl
	rm -rf $(LIBDIR) $(SHAREDIR)
	rm -f $(APPDIR)/fan-control.desktop $(ICONDIR)/fan-control.svg
	rm -f $(METAINFODIR)/org.community.FanControl.metainfo.xml
	rm -f $(POLICYDIR)/org.community.FanControl.policy
	rm -f $(UNITDIR)/fan-daemon.service
	rm -f $(DESTDIR)/usr/lib/tmpfiles.d/fan-control.conf
	rm -f $(DESTDIR)/usr/lib/sysusers.d/fan-control.conf
