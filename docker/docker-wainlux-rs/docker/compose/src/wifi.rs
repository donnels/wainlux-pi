//! WiFi network to the text phones expect in a QR: WIFI:T:<sec>;S:<ssid>;P:<pass>;H:<hidden>;;

/// Escape the characters the WIFI: format reserves.
fn esc(s: &str) -> String {
    s.chars()
        .flat_map(|c| match c {
            '\\' | ';' | ',' | ':' | '"' => vec!['\\', c],
            _ => vec![c],
        })
        .collect()
}

/// `security`: WPA, WEP or nopass. An open network drops the password.
pub fn text(ssid: &str, password: &str, security: &str, hidden: bool) -> Result<String, String> {
    if ssid.is_empty() {
        return Err("SSID is required".into());
    }
    let sec = match security.to_ascii_uppercase().as_str() {
        "WPA" | "WPA2" | "WPA3" => "WPA",
        "WEP" => "WEP",
        "NOPASS" | "OPEN" | "" => "nopass",
        other => return Err(format!("unknown WiFi security '{other}' (WPA, WEP, nopass)")),
    };
    let mut t = format!("WIFI:T:{sec};S:{};", esc(ssid));
    if sec != "nopass" {
        if password.is_empty() {
            return Err(format!("{sec} needs a password"));
        }
        t += &format!("P:{};", esc(password));
    }
    if hidden {
        t += "H:true;";
    }
    Ok(t + ";")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wpa_network() {
        assert_eq!(text("Guest", "s3cret", "WPA", false).unwrap(), "WIFI:T:WPA;S:Guest;P:s3cret;;");
    }

    #[test]
    fn personal_security_labels_use_the_wpa_qr_token() {
        for security in ["WPA2", "WPA3"] {
            assert_eq!(text("Guest", "s3cret", security, false).unwrap(), "WIFI:T:WPA;S:Guest;P:s3cret;;");
        }
    }

    #[test]
    fn reserved_characters_are_escaped() {
        assert_eq!(text("a;b", "c:d\\", "wpa", true).unwrap(), "WIFI:T:WPA;S:a\\;b;P:c\\:d\\\\;H:true;;");
    }

    #[test]
    fn open_network_has_no_password() {
        assert_eq!(text("Cafe", "ignored", "nopass", false).unwrap(), "WIFI:T:nopass;S:Cafe;;");
    }

    #[test]
    fn bad_input_is_refused() {
        assert!(text("", "x", "WPA", false).is_err());
        assert!(text("x", "", "WPA", false).is_err());
        assert!(text("x", "y", "WPA9", false).is_err());
    }
}
