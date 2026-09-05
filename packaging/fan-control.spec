Name:           fan-control
Version:        2026.9.1
Release:        1%{?dist}
Summary:        Safety-focused Linux fan-control daemon and native WebKitGTK workstation

License:        MIT
URL:            https://github.com/vindeckyy/fan-control
Source0:        %{name}-%{version}.tar.gz

BuildArch:      x86_64
BuildRequires:  python3-devel
BuildRequires:  nodejs
BuildRequires:  npm
BuildRequires:  systemd-rpm-macros

Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4
Requires:       webkitgtk6.0
Recommends:     polkit
Requires:       systemd

Recommends:     tuxedo-drivers

%description
Fan Control provides automatic temperature curves, direct manual control,
live CPU/GPU telemetry, dual-axis history graphs, and a native desktop
dashboard for compatible Clevo and Tongfang embedded controller interfaces.

%prep
%autosetup

%build
%make_build ui

%check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v
cd ui && npm test

%install
%make_install PREFIX=/usr
mkdir -p %{buildroot}%{_sysconfdir}/fan-control.d


%pre
getent group fan-control >/dev/null || groupadd -r fan-control
%post
%systemd_post fan-daemon.service

%preun
%systemd_preun fan-daemon.service

%postun
%systemd_postun_with_restart fan-daemon.service

%files
%license LICENSE
%doc README.md ROADMAP.md
%{_bindir}/fan-daemon
%{_bindir}/fan-gui
%{_bindir}/fan-ctl
%{_prefix}/lib/%{name}
%{_datadir}/%{name}
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
%{_datadir}/metainfo/org.community.FanControl.metainfo.xml
%{_datadir}/polkit-1/actions/org.community.FanControl.policy
%{_unitdir}/fan-daemon.service
%{_prefix}/lib/tmpfiles.d/%{name}.conf

%changelog
* Thu Sep 03 2026 Fan Control Community <fan-control@community.org> - 2026.9.1-1
- Initial RPM packaging release.
