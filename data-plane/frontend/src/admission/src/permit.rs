// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Transferable ownership of admission resources.

/// Algorithm-owned resources backing an admission permit. Drop releases the remaining units.
/// Implementations transfer ownership when splitting; they must not acquire capacity again.
pub trait AdmissionReservation: Send {
    /// Transfers one already-reserved batch unit to a generation child.
    fn split_one(&mut self) -> Box<dyn AdmissionReservation>;
}

/// Ownership transferred from an admission rule through preprocessing and execution.
/// Default permits represent accepted work with no resources to release.
#[derive(Default)]
pub struct AdmissionPermit {
    reservation: Option<Box<dyn AdmissionReservation>>,
}

impl AdmissionPermit {
    /// Takes ownership of a rule's complete reservation, releasing it when the permit is dropped.
    pub fn new(reservation: impl AdmissionReservation + 'static) -> Self {
        Self {
            reservation: Some(Box::new(reservation)),
        }
    }

    /// Reports whether work must retain this permit until its reserved resources can be released.
    pub fn is_reserved(&self) -> bool {
        self.reservation.is_some()
    }

    /// Transfers one reserved unit to a child; unrestricted permits remain resource-free.
    pub fn split_one(&mut self) -> Self {
        Self {
            reservation: self
                .reservation
                .as_mut()
                .map(|reservation| reservation.split_one()),
        }
    }
}
