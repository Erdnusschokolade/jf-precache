# Maintainer: Erdnusschokolade <96622762+Erdnusschokolade@users.noreply.github.com>
pkgname=jf-precache
pkgver=0.4.0
pkgrel=1
pkgdesc='Jellyfin playback webhook that pre-warms media files into RAM (page cache / ZFS ARC)'
arch=('any')
url='https://github.com/Erdnusschokolade/jf-precache'
license=('MIT')
depends=('python' 'python-requests' 'coreutils' 'tar')
backup=('etc/jf-precache/jf-precache.conf')
source=('jf-precache.py'
        'jf-precache.service'
        'jf-precache.conf.example'
        '10-jf-precache-mark-for-restart.hook'
        'LICENSE'
        'README.md')
sha256sums=('e31b0ae6192ff060fb2a9a0b4047e8d912713a75d4ea2d19e1ba1afcf6bf618e'
            '3798f6f6b1786dcb70c1afc370d3ccfa394a12d4f2fb90e43d637db8ae1a8f24'
            '0ae18d0c13e4415f50c49e0f7560ef03aae71cee26d3bdb03ca6e6d9b8dfd5d6'
            '934a22940a18e6281f4e2c0e0ff7e5e1d4b89bda9ce1d1368b409d0810570d20'
            '0322fb25917838ecfc55cf78264d5e77759c47ac49082c442d08cf7244c966c9'
            '4ffbdeab73c165e2cd4ce401de47b1907fb7adf3b1e57bffe2f08e2a838effcb')

package() {
  cd "$srcdir"
  install -Dm755 jf-precache.py      "$pkgdir/usr/bin/jf-precache"
  install -Dm644 jf-precache.service "$pkgdir/usr/lib/systemd/system/jf-precache.service"
  install -Dm644 10-jf-precache-mark-for-restart.hook "$pkgdir/usr/share/libalpm/hooks/10-jf-precache-mark-for-restart.hook"
  install -Dm600 jf-precache.conf.example "$pkgdir/etc/jf-precache/jf-precache.conf"
  install -Dm644 LICENSE   "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
